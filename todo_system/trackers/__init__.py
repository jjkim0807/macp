"""External task-tracker adapters (design `docs/design/state-store.md` §⑨).

A tracker (Jira, GitHub Issues, Linear, …) is a **view/mirror** of the task
markdown, which stays the source of truth ([[Decisions/2026-10-13-macp-task-store]]).
This package holds the transport-neutral contract (:mod:`.base`) and one concrete
adapter per provider (:mod:`.jira`, :mod:`.github_issues`, :mod:`.linear`). The package import
itself loads only :mod:`.base` — adapters are imported explicitly by their callers.

Stdlib-only on purpose: the adapters ship inside the public engine, so nothing
here may add a third-party dependency. HTTP transport is ``urllib.request``; the
only intra-repo import is :mod:`todo_system.paths` (for the non-secret config and
vault-root resolvers), never :mod:`todo_system.cli` — importing ``cli`` never
imports this package (the CLI reaches it through an in-function local import).
"""

from __future__ import annotations

from .base import (
    TaskRecord,
    TrackerAdapter,
    TrackerError,
    UpsertResult,
    adf_doc,
    append_progress_log,
    extract_adf_text,
    github_api_url,
    github_issues_repo,
    is_cancelled_status,
    jira_base_url,
    jira_project,
    linear_api_url,
    linear_team,
    load_github_token,
    load_jira_credentials,
    load_linear_api_key,
    map_status_name,
    parse_frontmatter,
    set_scalar,
    upsert_task_record,
)

__all__ = [
    "TaskRecord",
    "TrackerAdapter",
    "TrackerError",
    "UpsertResult",
    "adf_doc",
    "append_progress_log",
    "extract_adf_text",
    "github_api_url",
    "github_issues_repo",
    "is_cancelled_status",
    "jira_base_url",
    "jira_project",
    "linear_api_url",
    "linear_team",
    "load_github_token",
    "load_jira_credentials",
    "load_linear_api_key",
    "map_status_name",
    "parse_frontmatter",
    "set_scalar",
    "upsert_task_record",
]
