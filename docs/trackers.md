# External task trackers (Jira, GitHub Issues)

macp can mirror tasks to and from an external tracker. **The task markdown under
`Todo/items/` stays the source of truth** — the tracker is a view. Sync is
**opt-in**: with no tracker configured, `tracker pull` / `tracker push` do nothing
and make no network call.

There are two adapters: **Jira Cloud (REST v3)** (`todo_system/trackers/jira.py`)
and **GitHub Issues (REST)** (`todo_system/trackers/github_issues.py`, see
[GitHub Issues](#github-issues) below). The contract
(`todo_system/trackers/base.py`) is transport-neutral, so a Linear (GraphQL) adapter
is a follow-up leaf on the same interface. Design: `docs/design/state-store.md` §⑨.

## Configure (Jira)

Two non-secret settings, resolved env → nearest `.macp/config.toml` → unset (the
same ladder as `repo_slug` / `state_backend` in `paths.py`):

| Setting | `.macp/config.toml` key | Env override |
|---|---|---|
| Jira site base URL (`https://<site>.atlassian.net`) | `jira_base_url` | `MACP_JIRA_BASE_URL` |
| Jira project key (e.g. `PROJ`) | `jira_project` | `MACP_JIRA_PROJECT` |

Credentials are **secret** and live only in `.secrets/` or the environment — never
in config, logs, commits, or generated md:

- `<vault>/.secrets/jira.json` → `{"email": "you@example.com", "api_token": "…"}`, **or**
- env `MACP_JIRA_EMAIL` + `MACP_JIRA_API_TOKEN`.

Jira Cloud uses HTTP Basic auth with `email:api_token`. Create an API token at
`https://id.atlassian.com/manage-profile/security/api-tokens`. If the base URL,
project, **or** credentials are missing, the adapter is never built and sync is a
no-op.

## Run (Jira)

```bash
# Import tracker issues into Todo/items/ (idempotent upsert). --since is optional.
uv run python -m todo_system.cli tracker pull [--since 2026-10-01]

# Push done tasks (+ merged PR URL) to the tracker.
uv run python -m todo_system.cli tracker push
```

`pull` is the polling entry point a cron/worker tick calls (webhooks are a possible
future back-stop, not wired here — §⑨). `push` reads the orchestrator ledger
**read-only** for `done` tasks whose md carries a `jira:` `external_ref`, transitions
the Jira issue to *Done* and posts a comment with the merged PR URL; it never writes
the ledger.

## Field mapping (Jira → md)

| Concept | Jira (REST v3) | → task md |
|---|---|---|
| Title | `fields.summary` | body H1 (`# …`) |
| Description | `fields.description` (ADF) | md body (best-effort text, see limits) |
| Status | `fields.status.name` | `status` (table below) |
| Labels | `fields.labels[]` | `tags[]` |
| Due | `fields.duedate` | `due` (YYYY-MM-DD) |
| Priority | `fields.priority.name` | `priority` (Highest 90 / High 70 / Medium 50 / Low 30 / Lowest 10) |
| Issue key | `key` (e.g. `PROJ-123`) | `external_ref: jira:PROJ-123` |
| Assignee | `fields.assignee` | not mapped (single-worker model) |

## Status mapping (Jira)

| Tracker status | → task `status` |
|---|---|
| To Do / Open / In Progress / Backlog | `open` |
| Done / Closed / Resolved | `done` |
| Won't Do / Cancelled | `done` (+ `cancelled` tag) |

An unknown status name reads as `open` (not yet started). The task md's `status` is
2-valued (`open` / `done`); the orchestration state machine (ledger) is a separate
space the tracker never touches.

## Conflict rule — md wins

Sync writes two tracker-owned frontmatter fields: `external_ref` (the issue key) and
`external_updated_at` (the remote `updated` observed at the last successful sync).
On an existing task, `pull`:

- fills only **empty** md-owned fields from the remote;
- applies `status` as **monotonic "done promotion"** — if either side is `done` the
  task is `done`; **reopening happens only from the md**;
- on a genuine divergence — the remote advanced since `external_updated_at` **and** an
  md-owned field (title / due / priority) differs — **keeps the md** and appends a
  human-readable note to `## Progress Log` naming the field and the tracker's value,
  rather than overwriting.

A repeated pull of an unchanged remote rewrites nothing (the new content is written
only when it differs), so it never duplicates a task or churns the file.

## GitHub Issues

The GitHub adapter follows the same contract, opt-in gate, md-wins conflict rule
and secret hygiene as Jira. Official REST reference:
`https://docs.github.com/en/rest/issues` (checked 2026-10-25) — issues
(`…/rest/issues/issues`) and comments (`…/rest/issues/comments`).

### Configure

Two non-secret settings, resolved env → nearest `.macp/config.toml` → unset:

| Setting | `.macp/config.toml` key | Env override |
|---|---|---|
| Target repo (`owner/repo`) | `github_issues_repo` | `MACP_GITHUB_ISSUES_REPO` |
| API base URL (default `https://api.github.com`; GitHub Enterprise Server `https://<host>/api/v3`) | `github_api_url` | `MACP_GITHUB_API_URL` |

The token is **secret** and lives only in `.secrets/` or the environment:

- `<vault>/.secrets/github.json` → `{"token": "…"}`, **or**
- env `MACP_GITHUB_TOKEN`.

Use a fine-grained personal access token scoped to the target repo with
**Issues: read and write**. The generic `GITHUB_TOKEN` env and the `gh auth` token
are **deliberately not picked up** — an ambient token must not switch sync on. If
the repo is missing or not `owner/repo`, or there is no token, the adapter is never
built and sync is a no-op (no network call).

Every request sends `Authorization: Bearer <token>`,
`Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28` and a
`User-Agent`; the per-call timeout is `GITHUB_TIMEOUT_S`.

### Use

The `tracker pull` / `tracker push` CLI is still wired to Jira only; a CLI provider
switch is a follow-up. For now the GitHub adapter is a library API:

```python
from pathlib import Path

from todo_system.trackers.base import upsert_task_record
from todo_system.trackers.github_issues import GitHubIssuesAdapter

adapter = GitHubIssuesAdapter.from_config()   # None when not configured
if adapter is not None:
    for record in adapter.pull(since="2026-10-01"):
        upsert_task_record(record, Path("Todo/items"))
```

`pull` reads `GET /repos/{owner}/{repo}/issues` with `state=all`, `sort=updated`,
`direction=desc`, `per_page=100` and the optional `since` (a bare `YYYY-MM-DD` is sent
as `YYYY-MM-DDT00:00:00Z`). That endpoint also returns pull requests; items with a
`pull_request` key are skipped.

`push` acts only on a task whose `external_ref` is `github:owner/repo#N` — the repo in
the ref is the target, not the configured one:

- `status: done` → `PATCH /repos/{owner}/{repo}/issues/{N}` with
  `{"state": "closed", "state_reason": "completed"}`;
- a merged PR URL → `POST /repos/{owner}/{repo}/issues/{N}/comments` with
  `{"body": "macp: PR merged — <PR URL>"}`;
- neither → no request. A ref that is not `github:owner/repo#N` raises
  `TrackerError` (no request sent) when there is something to push.

### Field mapping (GitHub → md)

| Concept | GitHub Issues (REST) | → task md |
|---|---|---|
| Title | `title` | body H1 (`# …`) |
| Body | `body` (Markdown) | md body, verbatim |
| Status | `state` + `state_reason` | `status` (table below) |
| Labels | `labels[].name` | `tags[]` |
| Due | `milestone.due_on` | `due` (date part, YYYY-MM-DD) — an approximation |
| Priority | priority labels (below) | `priority` |
| Issue number | `number` | `external_ref: github:owner/repo#N` |
| Assignees | `assignees` | not mapped (single-worker model) |

GitHub has no native priority, so it is read from label conventions
(case-insensitive): `priority:<name>`, `priority/<name>` or `priority-<name>` with
`<name>` = highest 90 / high 70 / medium 50 / low 30 / lowest 10, and `P0` 90 / `P1` 70
/ `P2` 50 / `P3` 30 / `P4` 10. When several match, the highest wins. Priority labels
also stay in `tags`, so nothing is lost.

### Status mapping (GitHub)

| `state` / `state_reason` | → task `status` |
|---|---|
| `open` | `open` |
| `closed` / `completed` (or no reason) | `done` |
| `closed` / `not_planned` or `duplicate` | `done` (+ `cancelled` tag) |

## Limits

- **ADF is best-effort.** Jira v3 descriptions and comments are ADF (JSON), and there
  is no vetted ADF↔markdown library we may add (engine stays dependency-free). `pull`
  concatenates ADF text nodes into plain text; `push` wraps the PR URL in a minimal
  ADF document. Rich formatting (tables, panels, mentions) is lost on the way in.
- **Polling, not webhooks.** No inbound HTTPS endpoint; latency is the tick interval.
- **Two providers.** Jira + GitHub Issues; Linear is a follow-up leaf on the same
  `TrackerAdapter` interface.
- **The CLI is Jira-wired.** `tracker pull` / `tracker push` drive Jira only; GitHub
  Issues is a library API until a CLI provider switch lands.
- **One scalar `external_ref` per task** (`jira:…` or `github:…`) — no multi-provider
  map, so a task mirrors to one tracker.
- **No issue creation on push.** Both adapters only update issues that already carry
  an `external_ref`.
- **GitHub: single page.** `pull` fetches one page of 100 (most recently updated
  first) and does not follow `Link` pagination; use `since` to stay within it.
- **GitHub: push always closes as `completed`.** Closing as `not_planned` from a
  `cancelled` tag is a follow-up.
- **GitHub: pinned to REST API version `2022-11-28`.** It is the version GitHub
  Enterprise Server also accepts; github.com supports it until 2028-03-10 (the newer
  `2026-03-10` is current — checked 2026-10-25). Bump `GITHUB_API_VERSION` before then.
- **GitHub: due is the milestone's `due_on`**, and priority comes from label
  conventions — GitHub has neither natively.
- **Transport is stdlib `urllib.request`** with a per-call timeout (`JIRA_TIMEOUT_S`,
  `GITHUB_TIMEOUT_S`); no third-party HTTP client. Errors surface as the shared
  `TrackerError` (`todo_system/trackers/base.py`) carrying only provider / method /
  path / HTTP status — never the token or Authorization header.
