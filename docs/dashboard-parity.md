# Dashboard parity: what the web page and the Android app render the same way

The dashboard has two clients: the web page served by `scripts/macp_dashboard.py`
and the Android app `macp-dashboard-android`. Both read the same `/api/status`.
The server makes every decision, and the clients only render what it sends.
This file is the engine-side contract for the parts a client must implement. The
operator vault keeps its own `docs/dashboard-parity.md`, and for these sections
that copy points here. The older contracts that code comments cite (the theme
tokens, the human `why` table, the queue) are still in the vault's copy.

## Running and waiting

A task used to show as "in progress" from the moment the orchestrator pushed it
into a worker's mailbox, although nothing ran until that worker took it out —
sometimes days later. The orchestrator now assigns a task only to a worker with
a free slot, and the page shows two groups: what a worker is executing, and
what is waiting, with the one line that says why.

### 1. The `running` and `waiting` fields in `/api/status`

Both are top-level lists and **additive**. An older server sends neither; a
client then falls back to `active` (for running) and `queue` (for waiting), as
the web page does. `active` and `queue` keep their old meaning.

```json
"running": [
  {"task": "demo-3-api", "title": "API", "project": "demo", "group": "demo",
   "state": "in_progress", "agent": "worker-default", "agent_is_default": true,
   "age": "12m", "updated": "2026-11-25T17:25:51+09:00"}
],
"waiting": [
  {"slug": "demo-4-ui", "title": "UI", "project": "demo", "group": "demo",
   "wait": "assigned", "reason": "`worker-default`에 배정됨 — 곧 시작",
   "agent": "worker-default", "since": "2026-11-25T17:30:02+09:00"},
  {"slug": "demo-5-docs", "title": "Docs", "project": "demo", "group": "demo",
   "wait": "no_slot", "reason": "`worker-default` 빈 슬롯 없음 (2/2 사용 중)",
   "agent": "", "since": "2026-11-24"}
]
```

`running` holds the ledger rows whose state is `accepted` or `in_progress`: a
worker took the task and its stages are executing. A row has the same keys as a
row of `active` (`task`, `title`, `project`, `group`, `state`, `agent`,
`agent_is_default`, `age`, `updated`, …). A `dispatched` row is **not** running.

`waiting` holds every task that is not executing yet, in the order it will
start: first the tasks already assigned to a worker (ledger `dispatched`: handed
to a free slot, about to start), then the queue in the orchestrator's tick
order.

| key | type | render as |
|---|---|---|
| `slug`, `title`, `project`, `group` | string | The row's label, as in `queue`. |
| `wait` | string | Do not branch the layout on it. A stable id for a badge: `assigned`, or one of the queue's verdicts (`next_tick`, `no_slot`, `checkout_busy`, `gradle_cap`, `usage_hold`, `paused`, `budget`, `cooldown`, `capacity`). The list can grow, so handle an unknown value like any other. |
| `reason` | string | One line: why the task is not running yet. Always present. |
| `agent` | string | The worker an `assigned` row was given to; `""` for a queued row (it goes to whichever worker has room). |
| `since` | string | When the wait began (ISO date or date-time), or `""`. |

`no_slot` is new: the task's worker has no free slot. The reason names the
worker and its slots (`used/max`), or says its slot heartbeat stopped.

Every sentence is already in the server's UI language. A client never
translates or rewords it, and never works out a reason by itself.

### 2. Client rules (web and app alike)

1. Two groups: 「실행 중」 from `running`, 「대기 중」 from `waiting`. The first-screen
   count is `running.length`.
2. Draw `waiting` in the order received, with no sorting and no folding by
   project: the order is the information. Show `reason` under the title.
3. `queue_upcoming` (start date ahead) and `queue_blocked` (waiting on other
   tasks) stay as they were, under the waiting group.
4. Both lists are read-only: no buttons, no request of their own.
5. At a 390 px width there is no horizontal scroll.

### 3. Where each side lives

- The slot gate and the wait verdicts: `todo_system/orchestrator/orchestrator.py`
  (`_slot_gate`, `queue_snapshot`).
- Server wiring: `scripts/macp_dashboard.py`, where `collect_status` builds
  `running` and calls `collect_waiting` for `waiting`.
- Web client: `update` reads `d.running` and `d.waiting`; `queueRow` draws a
  waiting row.
- Android app: `macp-dashboard-android` renders the same two fields, tracked as
  a follow-up task.

## Triage cards

The attention list used to show only a slug and a short note. To find out why a
task stopped, someone had to read the ledger, the run artifacts and the task file
by hand. Now the server attaches a **triage card** to every row a person may need
to act on. The card says why the task stopped and offers buttons that run the
fix through the engine's existing paths.

The source of truth for the rules is the module docstring of
`todo_system/orchestrator/triage.py`. This section restates what a client needs.

### 1. The `triage` field in `/api/status`

The field appears on:

- every row of `attention` (ledger state `failed` or `awaiting_human`;
  deferred rows never reach this list), and
- every row of `human` whose `why` is `"parked"`.

Rows of `recent` are the same objects as the attention rows, so they can carry
the field too. Active rows, deferred rows and other human rows never have it.
The field is **additive**. When it is absent (an older server, a closed task,
or a diagnosis that failed), render the row exactly as before.

```json
"triage": {
  "rule": "review_pr",
  "reason": "PR이 열려 있고 머지만 남았어요",
  "evidence": ["PR: https://github.com/example/app/pull/12",
               "검증: PASS (20261120T154935-demo)",
               "auto_merge: false — 사람 머지를 기다리는 설정"],
  "advice": "검토하고 [머지]하세요",
  "actions": [{"id": "merge", "label": "머지"},
              {"id": "instruct", "label": "지시 추가"},
              {"id": "cancel", "label": "취소"}]
}
```

| key | type | render as |
|---|---|---|
| `rule` | string | Do not render it. It is a stable id for tests and analytics. Never branch the layout on it. |
| `reason` | string | One sentence in bold: why the task stopped. |
| `evidence` | string[] (0–6) | Muted lines, in order: what the server saw (note, PR, verdict, agent, …). |
| `advice` | string | One sentence: the recommended next step. It may name a manual step no button can do. |
| `actions` | {id, label}[] (0–3) | One button per entry, in order, with `label` as its text. The list may be empty. |

Every sentence is already in the server's UI language. A client never
translates, rewords or completes it.

`rule` is one of `merged_blocked`, `orphan_redelivery`, `workflow_scope`,
`nonff_push`, `remote_no_pr`, `human_misroute`, `reaped_off`, `ui_merge_hold`,
`device_unverified`, `review_pr`, `rescope_pending`, `timeout_park`, `blocked`,
`result_no_pr` or `fallback`. The list can grow, so handle an unknown value like
any other.

### 2. `POST /api/action`

A button posts the card's task and the chosen action:

```http
POST /api/action
Content-Type: application/json
Authorization: Bearer <dashboard token>        (the web page sends its cookie instead)

{"task": "<slug>", "action": "merge" | "resume" | "cancel" | "instruct", "text": "<one line>"}
```

- The request needs the same auth as `GET /api/status`. Only
  `application/json` is accepted, and the body must be at most 4096 bytes.
- `text` is required for `instruct` and ignored otherwise. It must be one line
  (no control characters or newlines) of 1–500 characters after trimming.
- The server diagnoses the task **again at click time** and runs the action only
  if that fresh card still offers it. A stale screen therefore cannot act. For
  example, a second [재개] after the first one returns `409`.
- A fifth id, `answer`, goes to the same route but is not a triage card button.
  It is the reply to a decision card, with its own body (`option` or `text`)
  and its own click-time check. See [Decision cards](#decision-cards).

What each action runs (no new mechanism):

| action | path |
|---|---|
| `merge` | `gh pr view` on the card's PR. Only an OPEN PR whose head branch is `worker/<slug>` (or a `worker/<slug>-rN` retry branch) passes. Then `gh pr merge <n> --repo <owner/repo> --squash`, with no `--admin`, `--auto` or `--delete-branch`. The next orchestrator tick closes the task. |
| `resume` | `resume.schedule`, the body of `cli resume`. The next tick re-dispatches the task. |
| `cancel` | The task md gets `status: cancelled` and a Progress Log line, and that one file is committed in the vault on `main`. |
| `instruct` | A Progress Log line `- <date>: 사용자 지시(대시보드): <text>` is committed the same way, then a resume is scheduled after that commit. |

The response is always JSON `{"ok": bool, "message": string}`. The one
exception is the auth gate's own `401`/`429`, which are unchanged text/plain
replies.

| status | meaning | `ok` |
|---|---|---|
| 200 | The action ran. `ok` says whether it worked (a refused merge, a vault not on `main`, …). | true / false |
| 400 | Bad JSON, an unknown slug format, an action id outside the set, or bad `text`. For `answer`: a malformed or out-of-range `option`, bad `text`, or not exactly one of the two | false |
| 404 | No ledger row for the task. For `answer`: no task md | false |
| 409 | The fresh card does not offer this action. For `answer`: the task is not a decision that can be answered now | false |
| 413 / 415 | The body is too large, or the request is not JSON | false |
| 500 | The diagnosis itself crashed (see the dashboard log) | false |

`message` is always a sentence for the user. It never contains an HTTP status,
gh/git stderr or a path; those go to the server log only. Show it as it is.

### 3. Irreversible steps never get a button

The action ids form a closed set: `merge`, `resume`, `cancel`, `instruct` and
`answer`. There is no id for deploying, submitting, publishing, deleting or
acting in the user's name, and the server answers any other id with `400`
before it looks at the task. A task md with `human_gate: true` (an irreversible
step is still pending) keeps only `cancel`, and its advice tells the person to
do the step by hand.

`answer` is never one of a triage card's buttons. It is the reply to a decision
card, and it only records what was chosen
(see [An answer only records](#3-an-answer-only-records)).

### 4. Client rules (web and app alike)

1. Render only: `reason`, `evidence`, `advice`, and one button per `actions[]`
   entry with its `label`. Make no client-side judgment and add no extra buttons.
2. Show no confirm dialog. The click is the approval.
3. Disable the card's buttons while its request is in flight.
4. `instruct` opens a one-line input (max 500 characters) with a send button. An
   empty input shows a local hint and sends nothing.
5. Show the response `message` on the card in a success or error color. On a
   network failure or a reply that is not JSON, show a fixed local sentence
   (web: 「요청을 보내지 못했어요 — 연결을 확인하세요」). Never show the HTTP status.
6. A merge or cancel can remove the row on the next poll, so keep the message
   visible elsewhere for a while (web: a toast on the status card for 15 s).
7. Tapping inside the card must not open the task detail.
8. At a 390 px width there is no horizontal scroll.
9. Refresh after an action. The server invalidates its cached status, so the
   next poll already shows the new state.

### 5. Where each side lives

- Rules and the facts they read: `todo_system/orchestrator/triage.py` (`diagnose`,
  `gather`).
- Actions: `todo_system/orchestrator/triage_actions.py` (`run_action`).
- Server wiring: `scripts/macp_dashboard.py`, where `_attach_triage` (called by
  `collect_status`) adds the field and `Handler._post_action` serves the POST.
- Web client: `triBlock`, `triAct`, `triSend` and `triClick` in the page
  template, called from `attnRow` and `humanRow`.
- Android app: `macp-dashboard-android` renders the same fields, tracked as a
  follow-up task.

## Decision cards

Some tasks are finished except for one choice a person has to make. Such a task
used to get a calendar block, and the block kept moving while the answer took
ten seconds. Now the server attaches a **decision card** to the task's row: the
question, one button per option, and a one-line box for any other answer. One
click records the answer in the task file.

The source of truth is the "Decision cards" part of the module docstring of
`todo_system/orchestrator/triage.py`. This section restates what a client needs.

### 1. The `decision` field in `/api/status`

The field appears on a row of `human` when both hold:

- the row's `why` is `"owned"` or `"parked"`, and
- the task md raises a decision that can be answered now: it has
  `needs_decision: true` or the `decision` tag, its status is not closed, its
  `worker_mode` is not `blocked`, and its `not_before` is not still ahead.

Rows with `why: "blocked"` and rows of `human_later` never have it (it is not
that person's turn yet), and no other list carries it. The field is
**additive**. When it is absent (an older server, an answered task, or a card
that failed to build), render the row exactly as before. A `parked` row can
carry both `decision` and `triage`; draw the decision card first.

```json
"decision": {
  "question": "Pick a budget plan",
  "options": ["(a) raise the cap to 30",
              "(b) split a separate budget: API only",
              "(c) hold"]
}
```

| key | type | render as |
|---|---|---|
| `question` | string | One sentence in bold. It is the task's `next_action` as one line (at most 300 characters). It may be empty; then draw no question line. |
| `options` | string[] (0–6) | One button per entry, in order, with the entry as its text, unchanged. Each is one line of at most 200 characters. The list may be empty; the text box is then the only way to answer. |

Both are the task author's words. A client never translates, rewords,
shortens, sorts or filters them.

The server also decides where the row goes, so a client keeps drawing rows in
the order and sections it already uses:

- the row has `urgent: true`, whatever its due date or time block, so it is
  counted in the first-screen decision number and drawn in the urgent section;
- rows with a card come first in `human`;
- the row has `group: ""`, so it is never folded under a project header.

### 2. `POST /api/action` with `action: "answer"`

The card answers through the same route as the triage buttons, with the same
auth, content type and size limit:

```http
POST /api/action
Content-Type: application/json
Authorization: Bearer <dashboard token>

{"task": "<slug>", "action": "answer", "option": 2}
```

or, for an answer that is not one of the options:

```json
{"task": "<slug>", "action": "answer", "text": "<one line>"}
```

- Send **exactly one** of `option` and `text`.
- `option` is the position of the chosen entry in the `options` the server
  sent, **counted from 1**. It must be a JSON integer.
- `text` is one line (no control characters or newlines) of 1–500 characters
  after trimming.
- The server reads the task md **again at click time**. A card that was
  answered a moment ago, or a task that was closed, returns `409`. The reply
  `message` says which choice was recorded, so a client can show it as is.

What the server writes, as one commit of the one task file in the vault on
`main`:

- a Progress Log line `- <date>: 결정(대시보드): <the option's text, or the
  answer>` (`decision (dashboard):` when the server's UI language is English),
  with the chosen words verbatim;
- `needs_decision: false`, and the `decision` tag and `decision_options` are
  removed, so the card is gone on the next poll;
- `next_action` becomes a short sentence naming the choice, and `updated` moves.

When the task is assigned to a human agent and that assignment is still
waiting (ledger `kind: human`, state `awaiting_human`), the server also sends
the orchestrator **one** reply of type `needs-input` carrying the answer. The
orchestrator stores it as the row's note and leaves the row waiting. A client
does nothing for this.

The response is JSON `{"ok": bool, "message": string}` under the rules of
[Triage cards §2](#2-post-apiaction):

| status | meaning | `ok` |
|---|---|---|
| 200 | The answer was handled. `ok` is false when nothing was recorded (a vault not on `main`, a busy vault, …), and also when the record was committed but the reply to the orchestrator could not be sent; `message` says which. | true / false |
| 400 | `option` is not an integer ≥ 1 or is past the end of the list, `text` is malformed, or the body does not carry exactly one of the two | false |
| 404 | No task md for that slug | false |
| 409 | The task is not a decision that can be answered now (already answered, closed, blocked, not yet due) | false |
| 413 / 415 | The body is too large, or the request is not JSON | false |
| 500 | The card could not be read (see the dashboard log) | false |

### 3. An answer only records

The button writes the choice down. It does nothing else, whatever the chosen
words say.

- The server does not merge, resume, cancel, dispatch or close anything on an
  answer. The task's `status` stays as it was, because the server cannot tell a
  choice that ends the task from one that leaves a step to do.
- An option may name a step that cannot be undone: a payment, a commitment, a
  deletion, a submission, a publication. Choosing it records that choice and
  runs nothing. Carrying it out belongs to a follow-up task or to the person,
  under the usual human gate.
- Handing an answered task to a worker stays a separate click: a task that is
  parked for a person offers [재개] on its triage card once the decision is
  recorded.
- An answer can be undone by editing the task md back (or reverting that one
  commit).

A client must not add behavior to an option, for example treating an option
that says "cancel" as the `cancel` action.

### 4. Client rules (web and app alike)

1. Render only: the `question`, one button per `options[]` entry with the entry
   as its text, and a one-line input (max 500 characters) with a send button.
   Make no client-side judgment, and add, drop or reorder no option.
2. Show no confirm dialog. The click is the answer.
3. Disable the card's buttons while its request is in flight.
4. An empty input shows a local hint and sends nothing.
5. Show the response `message` as it is: on the card when `ok` is false, and
   somewhere that survives the row's next redraw when `ok` is true (web: the
   status card's toast for 15 s), because an answered row loses its card on the
   next poll. On a network failure or a reply that is not JSON, show the fixed
   local sentence of Triage cards §4. Never show the HTTP status.
6. Tapping inside the card must not open the task detail.
7. At a 390 px width there is no horizontal scroll: a long option wraps inside
   its button.
8. Refresh after an answer. The server invalidates its cached status, so the
   next poll already shows the row without the card.

### 5. Writing a decision task (frontmatter)

An agent or a person raises a decision by writing the task file this way:

```yaml
needs_decision: true
next_action: "Pick a budget plan"
decision_options:
  - "(a) raise the cap to 30"
  - "(b) split a separate budget: API only"
  - "(c) hold"
```

- `needs_decision: true` (a plain `true` / `yes` / `1` / `on`) or the
  `decision` tag marks the task. Nothing else does: titles and prose are never
  read.
- `decision_options` is a list, one option per line. Quote each entry. At most
  six are sent, each cut to 200 characters. A one-line list `[a, b]` also
  works; quote an entry that contains a comma. Without the field the card has
  only the text box.
- The question is `next_action`.
- Leave `scheduled:` out. The calendar sync never puts a decision task on the
  calendar, deletes the event it already has, and never moves its block.
- The stage prompts tell agents to raise a leftover choice this way instead of
  burying the options in the Progress Log.

### 6. Where each side lives

- The card and the marker: `todo_system/orchestrator/triage.py`
  (`decision_card`, `md_decision`).
- The record and the reply: `todo_system/orchestrator/triage_actions.py`
  (`_answer`, reached through `run_action("answer", …)`).
- Server wiring: `scripts/macp_dashboard.py`, where `_attach_decisions` (called
  by `collect_status`) adds the field and orders the rows, and
  `Handler._post_answer` (called by `Handler._post_action`) serves the POST.
- Web client: `decBlock`, `decSend` and `decClick` in the page template, called
  from `humanRow`; they post through `triAct`.
- Calendar: `todo_system/calendar_sync.py` (`_is_decision_task`).
- Android app: `macp-dashboard-android` renders the same field, tracked as a
  follow-up task.

## Usage vs results

The usage guard says how much was spent. This card says on what, and what came
back: one table of project × kind of task, with output tokens, runs, merged
PRs, tasks done / cancelled / failed and tokens per merge, for the last 7 days
and for this month. The server aggregates and words everything. A client only
draws it. The same model is appended to one brief a week, so the brief and the
dashboard show the same numbers.

The source of truth is the module docstring of `todo_system/usage_value.py`.
This section restates what a client needs.

### 1. The `usage_value` field in `/api/status`

A top-level key, **additive**. When it is absent (an older server, or the
aggregation failed) draw no card and change nothing else.

```json
"usage_value": {
  "title": "사용량 대비 결과",
  "basis": "본부 run dir 기준 — 원격 러너 런과 정리된 run dir는 빠져 있어 출력 토큰은 하한이에요",
  "periods": [
    {"id": "week", "label": "지난 7일", "range": "11/25–12/1",
     "start": "2026-11-25", "end": "2026-12-01",
     "total": {"out_tokens": 548000, "out_tokens_text": "548k",
               "runs": 3, "runs_unmetered": 1, "merged_prs": 2,
               "done": 3, "cancelled": 0, "failed": 0,
               "tokens_per_merge": 274000, "tokens_per_merge_text": "274k",
               "text": "런 3 · 머지 PR 2 · 완료 3 · 취소 0 · 실패 0 · 머지당 274k · 미계측 런 1"},
     "rows": [
       {"project": "macp", "kind": "self", "kind_label": "macp 자기개선",
        "out_tokens": 466000, "out_tokens_text": "466k",
        "runs": 1, "runs_unmetered": 0, "merged_prs": 1,
        "done": 1, "cancelled": 0, "failed": 0,
        "tokens_per_merge": 466000, "tokens_per_merge_text": "466k",
        "text": "런 1 · 머지 PR 1 · 완료 1 · 취소 0 · 실패 0 · 머지당 466k"},
       {"project": "app", "kind": "bug", "kind_label": "버그",
        "out_tokens": 70000, "out_tokens_text": "70.0k",
        "runs": 1, "runs_unmetered": 1, "merged_prs": 0,
        "done": 1, "cancelled": 0, "failed": 0,
        "tokens_per_merge": null, "tokens_per_merge_text": "—",
        "text": "런 1 · 머지 PR 0 · 완료 1 · 취소 0 · 실패 0 · 머지당 — · 미계측 런 1"}]},
    {"id": "month", "label": "이번 달", "range": "12/1–12/1",
     "start": "2026-12-01", "end": "2026-12-01",
     "total": {"out_tokens": 0, "out_tokens_text": "0",
               "runs": 0, "runs_unmetered": 0, "merged_prs": 0,
               "done": 0, "cancelled": 0, "failed": 0,
               "tokens_per_merge": null, "tokens_per_merge_text": "—",
               "text": "런 0 · 머지 PR 0 · 완료 0 · 취소 0 · 실패 0 · 머지당 —"},
     "rows": []}
  ]
}
```

(The example is the 1st of a month: the week reaches back into the month
before, so the month can be empty while the week is not. The week's third row
is left out for length.)

| key | type | render as |
|---|---|---|
| `title` | string | The card's heading. |
| `basis` | string | One muted line under the heading: what was counted and why the tokens are a lower bound. |
| `periods` | object[] | Always two, `week` then `month`, in this order. |
| `periods[].id` | `"week"` \| `"month"` | Do not render it. A stable id. |
| `periods[].label`, `.range` | string | The period's heading, e.g. 「지난 7일 11/25–12/1」. |
| `periods[].start`, `.end` | `YYYY-MM-DD` | Do not render them. The same range as data. |
| `periods[].total` | cell | The period's summary: `out_tokens_text` and `text`. |
| `periods[].rows` | row[] | One line per row, **in the order received** (output tokens, high first). May be empty. |
| `rows[].project` | string | A project chip. A data value (a repo name, or `vault`), never translated. |
| `rows[].kind` | string | Do not render it. One of `discovery`, `self`, `feature`, `bug`, `security`, `research`, `ops`, `other`; the list can grow. |
| `rows[].kind_label` | string | The row's title, next to the chip. |
| `out_tokens_text` | string | The row's (or total's) output tokens, already shortened (`4.1M`). |
| `text` | string | The rest of the cell as one line: runs, merged PRs, done, cancelled, failed, tokens per merge, and the unmetered runs when there are any. |
| `out_tokens`, `runs`, `runs_unmetered`, `merged_prs`, `done`, `cancelled`, `failed`, `tokens_per_merge`, `tokens_per_merge_text` | number / string | The same cell as numbers, for a client that wants a table. `tokens_per_merge` is `null` (text `—`) when nothing was merged. |

Every string is already in the server's UI language, numbers included. A client
never formats a number, sorts or filters the rows, sums anything or translates
a label.

### 2. What the numbers mean

- A **run** is one run directory on the dashboard's host. A period is
  `[start, now]` in that host's local time: the last 7 calendar days, and the
  month so far.
- **Output tokens** are summed from each run's recorded stage usage. A run with
  no usable record adds 0 and is counted in `runs_unmetered`, so the sum is a
  lower bound.
- **Merged PRs** are distinct PR URLs a run reported as merged, plus the last
  PR of a task that is done now. Nothing is checked against GitHub.
- **Done / cancelled / failed** count distinct tasks that ran in the period, by
  their state now.
- **Kind** comes from the task itself, first match: born from a discovery
  report or a discovery run → `discovery`; the engine's own repo, tag or slug
  prefix → `self`; then a kind tag, a slug prefix, a hint tag; else `other`.

Not counted, and said so in `basis`: runs of a remote runner and run
directories already pruned (they stay on the machine that ran them). Also
missing: the PR of a vault delegate run (its URL is in no run directory, so
the task shows as done with no merge), and the usage of the planner, the brief
and interactive sessions.

### 3. Client rules (web and app alike)

1. Render only what the server sends. No arithmetic, no sorting, no number
   formatting, no translation.
2. The card is collapsed by default and remembers whether it is open (web:
   `localStorage` key `mdash.sec.usage-value`).
3. Hide the whole card when the key is absent or every period has no rows.
4. A period with no rows shows the client's usual empty line.
5. The card is read-only: no buttons, no request of its own.
6. At a 390 px width there is no horizontal scroll.

### 4. Where each side lives

- Aggregation, wording and the brief section: `todo_system/usage_value.py`
  (`build`, `render_brief`; CLI `python -m todo_system.usage_value report`).
- Server wiring: `scripts/macp_dashboard.py`, where `collect_usage_value`
  (called by `collect_status`) adds the field. A failure there drops the key
  and logs one line.
- Web client: `setUsageValue`, `uvPeriod` and `uvRow` in the page template,
  called at the end of `update`.
- Brief: `scripts/brief.sh` calls `python -m todo_system.usage_value
  brief-section` after the agent wrote the brief and appends the section it
  prints. The section goes into one brief a week.
- Android app: `macp-dashboard-android` renders the same field, tracked as a
  follow-up task.
