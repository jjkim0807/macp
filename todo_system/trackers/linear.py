"""Linear GraphQL adapter (design §⑨).

Transport is stdlib ``urllib.request`` (no new dependency) speaking **GraphQL only**:
every call is one ``POST`` of ``{"operationName", "query", "variables"}`` to the
endpoint, which is constructor-injected (default ``https://api.linear.app/graphql``;
``linear_api_url`` / ``MACP_LINEAR_API_URL`` or a loopback test fixture override it).
Auth is a Linear **personal API key** sent as the raw ``Authorization: <key>`` header
(no ``Bearer`` prefix — OAuth access tokens are not supported).
:meth:`LinearAdapter.from_config` returns ``None`` when the team or key is missing —
the opt-in no-op: no adapter, no network call. Only ``MACP_LINEAR_API_KEY`` /
``.secrets/linear.json`` are read, never an ambient ``LINEAR_API_KEY``.

Secret hygiene: the key never appears in a raised
:class:`~todo_system.trackers.base.TrackerError` (it carries only the GraphQL
operation / HTTP status / GraphQL error ``code`` — never an error ``message`` or the
response body) nor on stdout.

``pull`` runs ``issues(filter: {team: {key: {eq}}, updatedAt: {gte}}, first: 100,
orderBy: updatedAt)`` and maps each node per the §⑨ field/status tables (status
follows ``state.type``). ``push`` looks the issue up by identifier (its UUID and the
team's workflow states), moves a done task to the team's lowest-position
``completed`` state with ``issueUpdate``, and posts the merged PR URL with
``commentCreate``. The four GraphQL documents are module constants so the schema
surface lives in one place. Reference: https://linear.app/developers
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from .base import (
    TaskRecord,
    TrackerAdapter,
    TrackerError,
    linear_api_url,
    linear_team,
    load_linear_api_key,
    map_status_name,
)

# Per-call HTTP timeout — bounded like jira.JIRA_TIMEOUT_S (design §②).
LINEAR_TIMEOUT_S = 30

DEFAULT_API_URL = "https://api.linear.app/graphql"

# Single page per pull (pagination is not implemented — same shape as GitHub's 100).
PULL_PAGE_SIZE = 100

# Linear `priority` 1 Urgent / 2 High / 3 Medium / 4 Low on the same 0..100 scale as
# Jira/GitHub. 0 = "No priority" is deliberately unmapped (the key is omitted).
_PRIORITY_TO_INT = {1: 90, 2: 70, 3: 50, 4: 30}

_REF_RE = re.compile(r"^linear:([A-Za-z][A-Za-z0-9_]*-\d+)$")
_TEAM_RE = re.compile(r"^[A-Za-z0-9_]+$")
_BARE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# GraphQL error codes are echoed into TrackerError — keep them to a safe charset.
_CODE_RE = re.compile(r"[^A-Za-z0-9_]")

PULL_QUERY = (
    "query MacpPullIssues($filter: IssueFilter!, $first: Int!) {"
    " issues(filter: $filter, first: $first, orderBy: updatedAt) {"
    " nodes { id identifier title description priority dueDate updatedAt"
    " state { name type } labels { nodes { name } } } } }"
)
ISSUE_QUERY = (
    "query MacpIssueForPush($id: String!) {"
    " issue(id: $id) { id identifier"
    " team { states { nodes { id name type position } } } } }"
)
UPDATE_MUTATION = (
    "mutation MacpIssueComplete($id: String!, $stateId: String!) {"
    " issueUpdate(id: $id, input: { stateId: $stateId }) { success } }"
)
COMMENT_MUTATION = (
    "mutation MacpCommentCreate($issueId: String!, $body: String!) {"
    " commentCreate(input: { issueId: $issueId, body: $body }) { success } }"
)


def _position(state: dict) -> float:
    pos = state.get("position")
    if isinstance(pos, (int, float)) and not isinstance(pos, bool):
        return float(pos)
    return float("inf")


class LinearAdapter(TrackerAdapter):
    """Linear GraphQL adapter over stdlib ``urllib.request``."""

    def __init__(
        self,
        team: str,
        api_key: str,
        *,
        api_url: str = DEFAULT_API_URL,
        timeout: float = LINEAR_TIMEOUT_S,
    ) -> None:
        self.team = team
        # GraphQL has one endpoint — the path IS the endpoint, so it is kept verbatim.
        self.api_url = api_url
        self.timeout = timeout
        self._auth = api_key

    @classmethod
    def from_config(
        cls, vault_root: Path | None = None, *, start: Path | None = None
    ) -> "LinearAdapter | None":
        """Build an adapter from config + secrets, or ``None`` when any is missing.

        The opt-in gate: ``None`` means pull/push are a no-op with **no network
        call** (the adapter is never constructed). A team key that is not a bare
        ``[A-Za-z0-9_]+`` token also reads as unconfigured. ``start`` anchors the
        non-secret config search; ``vault_root`` anchors the ``.secrets`` lookup
        (test seams)."""
        team = linear_team(start)
        if not (team and _TEAM_RE.match(team)):
            return None
        api_key = load_linear_api_key(vault_root)
        if not api_key:
            return None
        api_url = linear_api_url(start) or DEFAULT_API_URL
        return cls(team, api_key, api_url=api_url)

    # --- transport ---------------------------------------------------------- #

    def _graphql(self, operation: str, query: str, variables: dict) -> dict:
        body = {"operationName": operation, "query": query, "variables": variables}
        headers = {
            "Authorization": self._auth,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "macp-tracker",
        }
        req = urllib.request.Request(
            self.api_url, data=json.dumps(body).encode("utf-8"), headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                rawbody = resp.read()
        except urllib.error.HTTPError as exc:
            raise TrackerError(f"Linear {operation} -> HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise TrackerError(f"Linear {operation} -> network error") from None
        if not rawbody:
            raise TrackerError(f"Linear {operation} -> non-JSON response")
        try:
            payload = json.loads(rawbody)
        except ValueError:
            raise TrackerError(f"Linear {operation} -> non-JSON response") from None
        if not isinstance(payload, dict):
            raise TrackerError(f"Linear {operation} -> malformed response")
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            # Only the machine-readable code — an error `message` can echo request data.
            codes: set[str] = set()
            for err in errors:
                ext = err.get("extensions") if isinstance(err, dict) else None
                code = ext.get("code") if isinstance(ext, dict) else None
                if isinstance(code, str) and _CODE_RE.sub("", code):
                    codes.add(_CODE_RE.sub("", code))
            raise TrackerError(
                f"Linear {operation} -> HTTP {status} GraphQL error "
                f"({', '.join(sorted(codes)) or 'unknown'})"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise TrackerError(f"Linear {operation} -> malformed response")
        return data

    # --- mapping ------------------------------------------------------------ #

    def map_status(self, remote_status: str) -> str:
        return map_status_name(remote_status)

    def map_fields(self, remote: dict) -> dict:
        out: dict = {}
        tags: list[str] = []
        labels = remote.get("labels")
        nodes = labels.get("nodes") if isinstance(labels, dict) else None
        for label in nodes or []:
            name = label.get("name") if isinstance(label, dict) else None
            if isinstance(name, str) and name.strip():
                tags.append(name.strip())
        if tags:
            out["tags"] = tags
        due = remote.get("dueDate")
        if isinstance(due, str) and due:
            out["due"] = due[:10]
        prio = remote.get("priority")
        if isinstance(prio, int) and not isinstance(prio, bool) and prio in _PRIORITY_TO_INT:
            out["priority"] = _PRIORITY_TO_INT[prio]
        return out

    def _issue_to_record(self, issue: dict) -> TaskRecord:
        ref = f"linear:{issue.get('identifier')}"
        mapped = self.map_fields(issue)
        state = issue.get("state")
        state_type = str((state.get("type") if isinstance(state, dict) else None) or "")
        if state_type == "canceled":
            tags = list(mapped.get("tags") or [])
            if "cancelled" not in tags:
                tags.append("cancelled")
            mapped["tags"] = tags
        return TaskRecord(
            external_ref=ref,
            title=str(issue.get("title") or ref),
            body=str(issue.get("description") or ""),   # Linear stores Markdown — verbatim
            frontmatter=mapped,
            status=self.map_status(state_type),
            remote_updated_at=issue.get("updatedAt"),
        )

    # --- pull / push -------------------------------------------------------- #

    def pull(self, since: str | None = None) -> list[TaskRecord]:
        filt: dict = {"team": {"key": {"eq": self.team}}}
        if since:
            gte = f"{since}T00:00:00Z" if _BARE_DATE_RE.match(since) else since
            filt["updatedAt"] = {"gte": gte}
        data = self._graphql(
            "MacpPullIssues", PULL_QUERY, {"filter": filt, "first": PULL_PAGE_SIZE})
        issues = data.get("issues")
        nodes = issues.get("nodes") if isinstance(issues, dict) else None
        return [
            self._issue_to_record(node)
            for node in nodes or []
            if isinstance(node, dict)
            and isinstance(node.get("identifier"), str) and node["identifier"]
        ]

    @staticmethod
    def _parse_ref(ref: str) -> str:
        m = _REF_RE.match(ref or "")
        if not m:
            raise TrackerError(f"Linear push: unsupported external_ref {ref!r}")
        return m.group(1)

    def _mutate(self, operation: str, query: str, variables: dict, field_name: str) -> None:
        res = self._graphql(operation, query, variables).get(field_name)
        if not (isinstance(res, dict) and res.get("success") is True):
            raise TrackerError(f"Linear {operation} -> success false")

    def push(self, task: TaskRecord) -> str:
        if task.status != "done" and not task.pr_url:
            return task.external_ref
        identifier = self._parse_ref(task.external_ref)
        issue = self._graphql("MacpIssueForPush", ISSUE_QUERY, {"id": identifier}).get("issue")
        if not (isinstance(issue, dict) and isinstance(issue.get("id"), str) and issue["id"]):
            raise TrackerError(f"Linear push: issue {identifier} not found")
        issue_id = issue["id"]
        if task.status == "done":
            team = issue.get("team")
            states = team.get("states") if isinstance(team, dict) else None
            nodes = states.get("nodes") if isinstance(states, dict) else None
            completed = sorted(
                (s for s in nodes or []
                 if isinstance(s, dict) and s.get("type") == "completed"
                 and isinstance(s.get("id"), str) and s["id"]),
                key=_position,
            )
            if not completed:
                raise TrackerError(f"Linear push: no completed workflow state for {identifier}")
            self._mutate(
                "MacpIssueComplete", UPDATE_MUTATION,
                {"id": issue_id, "stateId": completed[0]["id"]}, "issueUpdate",
            )
        if task.pr_url:
            self._mutate(
                "MacpCommentCreate", COMMENT_MUTATION,
                {"issueId": issue_id, "body": f"macp: PR merged — {task.pr_url}"},
                "commentCreate",
            )
        return task.external_ref
