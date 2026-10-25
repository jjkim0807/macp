"""Jira Cloud REST v3 adapter (design §⑨).

Transport is stdlib ``urllib.request`` (no new dependency). The base URL is
constructor-injected so a test points it at a loopback fixture; HTTP Basic auth is
built from ``email:api_token`` (Jira Cloud's scheme). :meth:`JiraAdapter.from_config`
returns ``None`` when the base URL, project, or credentials are missing — the opt-in
no-op: no adapter, no network call.

Secret hygiene: the ``Authorization`` header and token never appear in a raised
:class:`TrackerError` (it carries only method / path / status) nor on stdout.

``pull`` reads issues via the enhanced-search endpoint ``/rest/api/3/search/jql``
(the current v3 search; the legacy ``GET /rest/api/3/search`` was retired in 2025) and
maps each per the §⑨ field/status tables. ``push`` resolves the issue's "Done"
transition and posts it plus a comment carrying the merged PR URL, wrapped in a
minimal ADF document.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .base import (
    TaskRecord,
    TrackerAdapter,
    TrackerError,
    adf_doc,
    extract_adf_text,
    is_cancelled_status,
    jira_base_url,
    jira_project,
    load_jira_credentials,
    map_status_name,
)

# Per-call HTTP timeout. A module constant like mailbox.GIT_TIMEOUT_* so a network
# backend's blocking call is always bounded (design §② "동기 호출 + 내부 타임아웃").
JIRA_TIMEOUT_S = 30

# Jira priority name → macp priority int (0..100, §⑨). Unknown names leave it unset.
_PRIORITY_TO_INT = {"highest": 90, "high": 70, "medium": 50, "low": 30, "lowest": 10}

# The issue fields pull requests (§⑨ field table).
_PULL_FIELDS = "summary,description,status,labels,duedate,priority,updated"

# A transition whose target (or own name) reads as one of these closes the issue.
_DONE_TRANSITION_TARGETS = frozenset({"done", "closed", "resolved", "complete", "completed"})


class JiraAdapter(TrackerAdapter):
    """Jira Cloud REST v3 adapter over stdlib ``urllib.request``."""

    def __init__(
        self,
        base_url: str,
        project: str,
        email: str,
        api_token: str,
        *,
        timeout: float = JIRA_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.project = project
        self.timeout = timeout
        raw = f"{email}:{api_token}".encode("utf-8")
        self._auth = "Basic " + base64.b64encode(raw).decode("ascii")

    @classmethod
    def from_config(
        cls, vault_root: Path | None = None, *, start: Path | None = None
    ) -> "JiraAdapter | None":
        """Build an adapter from config + secrets, or ``None`` when any is missing.

        The opt-in gate: ``None`` means pull/push are a no-op with **no network
        call** (the adapter is never constructed). ``start`` anchors the non-secret
        config search; ``vault_root`` anchors the ``.secrets`` lookup (test seams)."""
        base_url = jira_base_url(start)
        project = jira_project(start)
        creds = load_jira_credentials(vault_root)
        if not (base_url and project and creds):
            return None
        return cls(base_url, project, creds[0], creds[1])

    # --- transport ---------------------------------------------------------- #

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                rawbody = resp.read()
        except urllib.error.HTTPError as exc:
            raise TrackerError(f"Jira {method} {path} -> HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise TrackerError(f"Jira {method} {path} -> network error") from None
        if not rawbody:
            return {}
        try:
            return json.loads(rawbody)
        except ValueError:
            raise TrackerError(f"Jira {method} {path} -> non-JSON response") from None

    # --- mapping ------------------------------------------------------------ #

    def map_status(self, remote_status: str) -> str:
        return map_status_name(remote_status)

    def map_fields(self, remote: dict) -> dict:
        out: dict = {}
        labels = remote.get("labels")
        if isinstance(labels, list) and labels:
            out["tags"] = [str(x) for x in labels]
        due = remote.get("duedate")
        if due:
            out["due"] = str(due)
        prio_obj = remote.get("priority")
        prio = prio_obj.get("name") if isinstance(prio_obj, dict) else None
        if prio:
            mapped = _PRIORITY_TO_INT.get(str(prio).strip().lower())
            if mapped is not None:
                out["priority"] = mapped
        return out

    def _issue_to_record(self, issue: dict) -> TaskRecord:
        key = str(issue.get("key") or "")
        fields = issue.get("fields") or {}
        mapped = self.map_fields(fields)
        status_obj = fields.get("status")
        status_name = status_obj.get("name") if isinstance(status_obj, dict) else ""
        if is_cancelled_status(status_name or ""):
            tags = list(mapped.get("tags") or [])
            if "cancelled" not in tags:
                tags.append("cancelled")
            mapped["tags"] = tags
        return TaskRecord(
            external_ref=f"jira:{key}",
            title=str(fields.get("summary") or key),
            body=extract_adf_text(fields.get("description")),
            frontmatter=mapped,
            status=self.map_status(status_name or ""),
            remote_updated_at=fields.get("updated"),
        )

    # --- pull / push -------------------------------------------------------- #

    def pull(self, since: str | None = None) -> list[TaskRecord]:
        jql = f'project = "{self.project}"'
        if since:
            jql += f' AND updated >= "{since}"'
        jql += " ORDER BY updated DESC"
        query = urllib.parse.urlencode({"jql": jql, "fields": _PULL_FIELDS, "maxResults": 50})
        data = self._request("GET", f"/rest/api/3/search/jql?{query}")
        issues = data.get("issues") if isinstance(data, dict) else None
        return [self._issue_to_record(issue) for issue in (issues or [])]

    def push(self, task: TaskRecord) -> str:
        key = task.external_ref.split(":", 1)[1] if ":" in task.external_ref else task.external_ref
        if task.status == "done":
            transitions = self._request("GET", f"/rest/api/3/issue/{key}/transitions")
            tid = self._done_transition_id(transitions)
            if tid is not None:
                self._request(
                    "POST",
                    f"/rest/api/3/issue/{key}/transitions",
                    {"transition": {"id": tid}},
                )
        if task.pr_url:
            self._request(
                "POST",
                f"/rest/api/3/issue/{key}/comment",
                {"body": adf_doc(f"macp: PR merged — {task.pr_url}")},
            )
        return task.external_ref

    @staticmethod
    def _done_transition_id(transitions: dict) -> str | None:
        for t in transitions.get("transitions") or []:
            to = t.get("to") or {}
            to_name = str(to.get("name") or "").strip().lower()
            name = str(t.get("name") or "").strip().lower()
            if to_name in _DONE_TRANSITION_TARGETS or name in _DONE_TRANSITION_TARGETS:
                return t.get("id")
        return None
