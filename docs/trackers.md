# External task trackers (Jira)

macp can mirror tasks to and from an external tracker. **The task markdown under
`Todo/items/` stays the source of truth** — the tracker is a view. Sync is
**opt-in**: with no tracker configured, `tracker pull` / `tracker push` do nothing
and make no network call.

Today there is one adapter, **Jira Cloud (REST v3)**. The contract
(`todo_system/trackers/base.py`) is transport-neutral, so GitHub Issues (REST) and
Linear (GraphQL) adapters are follow-up leaves on the same interface. Design:
`docs/design/state-store.md` §⑨.

## Configure

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

## Run

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

## Status mapping

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

## Limits

- **ADF is best-effort.** Jira v3 descriptions and comments are ADF (JSON), and there
  is no vetted ADF↔markdown library we may add (engine stays dependency-free). `pull`
  concatenates ADF text nodes into plain text; `push` wraps the PR URL in a minimal
  ADF document. Rich formatting (tables, panels, mentions) is lost on the way in.
- **Polling, not webhooks.** No inbound HTTPS endpoint; latency is the tick interval.
- **One provider.** Jira only; GitHub Issues / Linear are follow-up leaves on the
  same `TrackerAdapter` interface.
- **Transport is stdlib `urllib.request`** with a per-call timeout (`JIRA_TIMEOUT_S`);
  no third-party HTTP client. Errors surface as `TrackerError` carrying only
  method / path / HTTP status — never the token or Authorization header.
