"""GitHub Issues REST adapter (design §⑨).

Transport is stdlib ``urllib.request`` (no new dependency). The API base URL is
constructor-injected (default ``https://api.github.com``; a GitHub Enterprise Server
``https://<host>/api/v3`` or a loopback test fixture override it); auth is a
``Bearer`` token. :meth:`GitHubIssuesAdapter.from_config` returns ``None`` when the
repo or token is missing — the opt-in no-op: no adapter, no network call. Only
``MACP_GITHUB_TOKEN`` / ``.secrets/github.json`` are read, never an ambient
``GITHUB_TOKEN`` or ``gh`` CLI token.

Secret hygiene: the ``Authorization`` header and token never appear in a raised
:class:`~todo_system.trackers.base.TrackerError` (it carries only method / path /
status) nor on stdout.

``pull`` reads ``GET /repos/{owner}/{repo}/issues`` (``state=all``, incremental
``since``) and maps each per the §⑨ field/status tables. That endpoint also returns
pull requests, so any item carrying a ``pull_request`` key is skipped. ``push``
closes the issue with ``PATCH /repos/{owner}/{repo}/issues/{n}`` and posts the merged
PR URL with ``POST /repos/{owner}/{repo}/issues/{n}/comments``. Reference:
https://docs.github.com/en/rest/issues
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .base import (
    TaskRecord,
    TrackerAdapter,
    TrackerError,
    github_api_url,
    github_issues_repo,
    load_github_token,
    map_status_name,
)

# Per-call HTTP timeout — bounded like jira.JIRA_TIMEOUT_S (design §②).
GITHUB_TIMEOUT_S = 30

# The REST API version header GitHub pins responses to. 2022-11-28 is accepted by
# github.com until 2028-03-10 and by GitHub Enterprise Server; 2026-03-10 is newer.
GITHUB_API_VERSION = "2022-11-28"

DEFAULT_API_URL = "https://api.github.com"

# Priority label conventions (GitHub has no native priority field, §⑨ "라벨 관행"):
# `priority:<name>` / `priority/<name>` / `priority-<name>` and `P0`..`P4`, mapped to
# the same 0..100 scale as Jira. The max over all matching labels wins.
_PRIORITY_NAME_TO_INT = {"highest": 90, "high": 70, "medium": 50, "low": 30, "lowest": 10}
_P_LABEL_TO_INT = {"p0": 90, "p1": 70, "p2": 50, "p3": 30, "p4": 10}
_PRIORITY_LABEL_RE = re.compile(r"^priority[:/-]\s*(\w+)$", re.IGNORECASE)

# `closed` with one of these `state_reason`s is done + a `cancelled` tag (§⑨ "취소").
_CANCELLED_REASONS = frozenset({"not_planned", "duplicate"})

_REF_RE = re.compile(r"^github:([^/\s]+)/([^#\s]+)#(\d+)$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BARE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _priority_from_label(name: str) -> int | None:
    low = name.strip().lower()
    if low in _P_LABEL_TO_INT:
        return _P_LABEL_TO_INT[low]
    m = _PRIORITY_LABEL_RE.match(low)
    return _PRIORITY_NAME_TO_INT.get(m.group(1)) if m else None


class GitHubIssuesAdapter(TrackerAdapter):
    """GitHub Issues REST adapter over stdlib ``urllib.request``."""

    def __init__(
        self,
        repo: str,
        token: str,
        *,
        base_url: str = DEFAULT_API_URL,
        timeout: float = GITHUB_TIMEOUT_S,
    ) -> None:
        self.repo = repo
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._auth = f"Bearer {token}"

    @classmethod
    def from_config(
        cls, vault_root: Path | None = None, *, start: Path | None = None
    ) -> "GitHubIssuesAdapter | None":
        """Build an adapter from config + secrets, or ``None`` when any is missing.

        The opt-in gate: ``None`` means pull/push are a no-op with **no network
        call** (the adapter is never constructed). A repo that is not ``owner/repo``
        also reads as unconfigured. ``start`` anchors the non-secret config search;
        ``vault_root`` anchors the ``.secrets`` lookup (test seams)."""
        repo = github_issues_repo(start)
        if not (repo and _REPO_RE.match(repo)):
            return None
        token = load_github_token(vault_root)
        if not token:
            return None
        base_url = github_api_url(start) or DEFAULT_API_URL
        return cls(repo, token, base_url=base_url)

    # --- transport ---------------------------------------------------------- #

    def _request(self, method: str, path: str, body: dict | None = None) -> object:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": self._auth,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
            "User-Agent": "macp-tracker",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                rawbody = resp.read()
        except urllib.error.HTTPError as exc:
            raise TrackerError(f"GitHub {method} {path} -> HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise TrackerError(f"GitHub {method} {path} -> network error") from None
        if not rawbody:
            return {}
        try:
            return json.loads(rawbody)
        except ValueError:
            raise TrackerError(f"GitHub {method} {path} -> non-JSON response") from None

    # --- mapping ------------------------------------------------------------ #

    def map_status(self, remote_status: str) -> str:
        return map_status_name(remote_status)

    def map_fields(self, remote: dict) -> dict:
        out: dict = {}
        tags: list[str] = []
        for label in remote.get("labels") or []:
            name = label.get("name") if isinstance(label, dict) else label
            if isinstance(name, str) and name.strip():
                tags.append(name.strip())
        if tags:
            out["tags"] = tags
        milestone = remote.get("milestone")
        due_on = milestone.get("due_on") if isinstance(milestone, dict) else None
        if isinstance(due_on, str) and due_on:
            out["due"] = due_on[:10]
        prios = [p for p in (_priority_from_label(t) for t in tags) if p is not None]
        if prios:
            out["priority"] = max(prios)
        return out

    def _issue_to_record(self, issue: dict) -> TaskRecord:
        ref = f"github:{self.repo}#{issue.get('number')}"
        mapped = self.map_fields(issue)
        state = str(issue.get("state") or "")
        if state == "closed" and issue.get("state_reason") in _CANCELLED_REASONS:
            tags = list(mapped.get("tags") or [])
            if "cancelled" not in tags:
                tags.append("cancelled")
            mapped["tags"] = tags
        return TaskRecord(
            external_ref=ref,
            title=str(issue.get("title") or ref),
            body=str(issue.get("body") or ""),
            frontmatter=mapped,
            status=self.map_status(state),
            remote_updated_at=issue.get("updated_at"),
        )

    # --- pull / push -------------------------------------------------------- #

    def pull(self, since: str | None = None) -> list[TaskRecord]:
        params = {"state": "all", "sort": "updated", "direction": "desc", "per_page": 100}
        if since:
            params["since"] = f"{since}T00:00:00Z" if _BARE_DATE_RE.match(since) else since
        query = urllib.parse.urlencode(params)
        data = self._request("GET", f"/repos/{self.repo}/issues?{query}")
        items = data if isinstance(data, list) else []
        return [
            self._issue_to_record(item)
            for item in items
            if isinstance(item, dict) and "pull_request" not in item
        ]

    @staticmethod
    def _parse_ref(ref: str) -> tuple[str, int]:
        m = _REF_RE.match(ref or "")
        if not m:
            raise TrackerError(f"GitHub push: unsupported external_ref {ref!r}")
        return f"{m.group(1)}/{m.group(2)}", int(m.group(3))

    def push(self, task: TaskRecord) -> str:
        if task.status != "done" and not task.pr_url:
            return task.external_ref
        # The repo named in the ref is the target — not the configured repo.
        repo, number = self._parse_ref(task.external_ref)
        if task.status == "done":
            self._request(
                "PATCH",
                f"/repos/{repo}/issues/{number}",
                {"state": "closed", "state_reason": "completed"},
            )
        if task.pr_url:
            self._request(
                "POST",
                f"/repos/{repo}/issues/{number}/comments",
                {"body": f"macp: PR merged — {task.pr_url}"},
            )
        return task.external_ref
