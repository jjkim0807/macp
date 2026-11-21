# Dashboard parity: what the web page and the Android app render the same way

The dashboard has two clients: the web page served by `scripts/macp_dashboard.py`
and the Android app `macp-dashboard-android`. Both read the same `/api/status`.
The server makes every decision, and the clients only render what it sends.
This file is the engine-side contract for the parts a client must implement. The
operator vault keeps its own `docs/dashboard-parity.md`, and for these sections
that copy points here. The older contracts that code comments cite (the theme
tokens, the human `why` table, the queue) are still in the vault's copy.

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
| 400 | Bad JSON, an unknown slug format, an action id outside the set, or bad `text` | false |
| 404 | No ledger row for the task | false |
| 409 | The fresh card does not offer this action | false |
| 413 / 415 | The body is too large, or the request is not JSON | false |
| 500 | The diagnosis itself crashed (see the dashboard log) | false |

`message` is always a sentence for the user. It never contains an HTTP status,
gh/git stderr or a path; those go to the server log only. Show it as it is.

### 3. Irreversible steps never get a button

The action ids form a closed set: `merge`, `resume`, `cancel` and `instruct`.
There is no id for deploying, submitting, publishing, deleting or acting in the
user's name, and the server answers any other id with `400` before it looks at
the task. A task md with `human_gate: true` (an irreversible step is still
pending) keeps only `cancel`, and its advice tells the person to do the step by
hand.

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
