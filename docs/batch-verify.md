# Batch verify

Batch verify lets small tasks share one run of a repo's full gate. A batched task
gets a quick verify of only its own changes and opens a ready PR. It then waits in
the ledger as `batch_ready`. When enough tasks wait, the engine merges them onto one
local integration branch and runs the repo's full gate on it once. If that passes,
it merges the PRs in order. A task that cannot be batched, or whose batch fails,
goes back to the ordinary individual full verify. Nothing is lost. It only takes
the slower path.

The feature is off by default and is turned on per repo. Without a config entry for
a repo, its tasks run exactly as before.

- [Flow](#flow)
- [Configuration](#configuration)
- [Defaults](#defaults)
- [Ledger states and history](#ledger-states-and-history)
- [Return reasons](#return-reasons)
- [Dashboard](#dashboard)
- [Limits](#limits)

## Flow

1. **Eligibility.** When a run starts, the first matching rule decides (`batch_gate.eligible`):
   - the repo has no batch gate → no;
   - a vault task (no `repo:`) → no;
   - the task was sent back once (`verify_full` on its payload) → no;
   - not the five-stage `worker_mode: auto` pipeline → no;
   - `auto_merge` off → no;
   - a full-gate command is marked to run on the remote node (`.macp/gates.json` in the repo) → no;
   - `verify_batch: false` → no; `verify_batch: true` → yes;
   - otherwise the model tier decides: `light` and `standard` are batched.
2. **Quick verify.** The Verify stage checks only what the branch changed. It is told
   which full-gate commands are deferred, plus the repo's `quick_hint` if it has one.
3. **Ready PR.** The Report stage opens a ready PR but does not merge it. The
   orchestrator moves the ledger row to `batch_ready` and records the PR url, the
   branch and the sha the quick verify judged. A task that asked for a device run
   (`emulator: true`) needs evidence of that run first, or it goes back.
4. **Trigger.** A gate starts for a repo once `MIN_BATCH` tasks wait, or once the
   oldest has waited `MAX_WAIT_S`. A paused worker holds back a new gate, and so
   does a full Gradle build cap. A gate already running is still judged and merged.
5. **Integration branch.** The oldest waiting tasks, at most `max_batch`, are merged
   in ready order onto a local-only branch `integ/<repo_name>/<timestamp>`. It is
   built in its own worktree of the shared clone. A member that conflicts, whose
   branch moved, or whose branch is gone is left out and goes back.
6. **One full gate.** The repo's `full_gate` commands run once on that branch as a
   detached job. Every command must exit 0.
7. **Merge or return.** If the gate passes, the PRs are squash-merged in build order
   and each task is closed (`status: done`, history `batch-merged`). Before each
   merge, the default branch must still be at the sha the gate saw. If it moved,
   the remaining members are rebuilt and gated again, up to `MAX_RESTARTS` times. If
   the gate fails or times out, every member goes back, with one alert for the batch.

## Configuration

The config is vault data, `<vault>/.macp/batch_gate.json`:

```json
{
  "enabled": true,
  "repos": {
    "app": {
      "full_gate": [["./gradlew", "test"], ["./gradlew", "lint"]],
      "quick_hint": "Run the unit tests of the touched module.",
      "max_wait_min": 30,
      "max_batch": 5,
      "gate_timeout_min": 120
    }
  }
}
```

The key under `repos` is the repo's name as the engine derives it from the url
(`repos.repo_name`). For example, `https://example.test/acme/app.git` → `app`.

| Key | Meaning |
|---|---|
| `full_gate` | Required. A list of argv lists, not shell strings. A single argv (`["./gradlew", "test"]`) is accepted too. An empty list, or an empty or non-string token, makes the whole entry unusable |
| `quick_hint` | Optional. Text appended to the quick-verify instruction |
| `max_wait_min` | Optional. Overrides `MAX_WAIT_S`, in minutes |
| `max_batch` | Optional. Overrides `MAX_BATCH` |
| `gate_timeout_min` | Optional. Overrides `GATE_TIMEOUT_S`, in minutes |

An override has to be a positive number, or the default stays. The feature is off
for a repo when any of these holds: there is no file, it cannot be read or parsed,
`enabled` is not true, the repo has no entry, or its `full_gate` is unusable.

Per task, frontmatter `verify_batch: true | false` overrides the tier rule (see
[Task files](tasks.md#field-reference)).

## Defaults

From `todo_system/orchestrator/batch_gate.py`:

| Constant | Value | Meaning |
|---|---|---|
| `EXPIRE_S` | 86400 (24 h) | A `batch_ready` row older than this goes back |
| `MIN_BATCH` | 2 | A gate starts once this many tasks wait… |
| `MAX_WAIT_S` | 1800 (30 min) | …or once the oldest has waited this long |
| `MAX_BATCH` | 5 | Tasks per integration branch |
| `MAX_RESTARTS` | 3 | Rebuilds after the default branch moved |
| `GATE_TIMEOUT_S` | 7200 (2 h) | One full-gate run |
| `MERGE_ATTEMPTS` | 3 | Failed merge calls before a member goes back |

The job record for each repo is host-local runtime state, kept in
`<vault>/Worker/_state/batch_gate/<repo_name>.json`.

## Ledger states and history

These strings are wire format. They land in the git-tracked assignment JSON.

| String | Kind | Meaning |
|---|---|---|
| `batch_ready` | ledger `state` | The run passed its quick verify and opened a ready PR. It waits for the shared full gate |
| `batch-ready` | history `type` | The row became `batch_ready` |
| `batch-merged` | history `type` | The gate passed and the PR merged. The row is `done` |
| `batch-returned` | history `type` | The row was sent back to an individual full verify. The entry carries `reason` |

A `batch_ready` row is not active, not parked, not deferred and not failed. It is not
dispatched again while it waits, the stale reaper leaves it alone, and nobody is
nudged about it.

A returned row is stored `failed` with `batch-returned` as its last history entry.
Every view shows it as `deferred`, because nothing failed and the run passed its
quick verify. It gets no refail, it is re-dispatched after the flat poll cooldown,
and it never poisons the task. Two payload keys carry over to the next run:
`verify_full: true` keeps the task out of the batch, and `batch_return_reason`
records why it came back.

## Return reasons

| Reason | Meaning |
|---|---|
| `not-enabled` | The repo's gate left the config |
| `expired` | No gate ran within `EXPIRE_S` |
| `no-device-evidence` | The task asked for a device run, and this run left no evidence of one |
| `no-pr` | The report opened no PR |
| `no-head-sha` | The judged sha could not be read |
| `quick-not-pass` | The quick verify did not say PASS |
| `gate-failed` | The shared full gate exited non-zero |
| `gate-timeout` | The shared full gate ran past its time limit |
| `conflict` | The branch does not merge onto the integration branch |
| `branch-moved` | Origin's branch is not the judged sha |
| `branch-missing` | Origin has no such branch |
| `no-checkout` | This host has no shared clone of the repo |
| `restarts` | The default branch kept moving |
| `merge-failed` | The PR would not merge after the gate passed |

## Dashboard

- **`/api/status`:**
  - Every ledger row (`recent`, `active`, `attention`, …) carries a bool `batch_ready`
    next to its display `state`, so a client that predates the state value can still
    tell.
  - The top-level `batch_ready[]` lists the open rows in state `batch_ready`. Rows
    whose todo is closed are dropped. Each row has the usual keys (`task`, `state`,
    `note`, `age`, `title`, `project`, …) plus `group`. These rows are never in
    `active` or `attention`.
- **Web page:** a "📦 Batch verify queue" card between "In progress" and "Queue". It
  is hidden when the list is empty. The state badge reads "batch wait".
- **`Todo/Orchestrator.md`:**
  - A "📦 묶음 검증 대기 (통합 게이트 후 머지)" section right after "진행 중". It is
    rendered only when such a row exists. With none, the board is unchanged.
  - A returned row appears under deferred with the label "묶음 복귀 — 개별 Verify 대기".

## Limits

- Repos whose full gate has a command marked to run on the remote node are left out.
  Those tasks verify individually.
- The integration branch is local-only and never pushed.
- There is no bisection. When the shared gate fails, every member goes back, including
  members that would pass alone. Each one then proves itself in its individual full
  verify.
- Beta publishing after a batch merge is not triggered by the batch itself. The
  orchestrator's beta sweep (unpublished main → beta catch-up) picks the merged
  commits up like any other merge.
- The gate's progress (job running, which members) is not shown on the dashboard yet.
  Only the waiting rows are.
- A task returned for `conflict` gets a retry note telling Implement that its own
  checks passed and the one thing to do is merge the default branch.
