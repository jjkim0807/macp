"""Tests for the Linear GraphQL adapter + the tracker contract on Linear (design §⑨).

Two layers, both offline against one local ``RecordingLinearServer``
(``127.0.0.1:0``, the ``test_trackers_github_issues.RecordingTrackerServer`` pattern
reduced to a single ``POST /graphql`` route dispatched on ``operationName``):

* **Linear transport** (``linear.LinearAdapter``) — pull mapping, idempotent upsert,
  push lookup/update/comment request bodies, GraphQL ``errors`` handling, the opt-in
  ``from_config`` gate, and key secrecy, asserted on the exact request JSON.
* **Tracker contract** — the seven ``test_contract_*`` checks every
  :class:`TrackerAdapter` owes the md bridge, mirrored here as ``[linear]``. Mirrored
  rather than imported: ``todo_system/tests`` is not a package, so importing
  ``test_trackers_github_issues``'s fixtures would depend on pytest's import mode, and
  its server has no GraphQL route.

The fixture responses are **not** real recordings — there are no live credentials —
but hand-written replicas of Linear's response shapes (``data.issues.nodes[]``,
``data.issue.team.states.nodes[]``, ``data.issueUpdate.success``,
``data.commentCreate.success``, ``errors[].extensions.code``).

Everything runs against ``tmp_path`` — never the real vault (the session-scoped
``_real_vault_untouched`` conftest guard backs this up).
"""

from __future__ import annotations

import importlib
import json
import threading
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

import pytest

from todo_system.trackers import base, linear
from todo_system.trackers.base import (
    TaskRecord,
    TrackerAdapter,
    TrackerError,
    parse_frontmatter,
    upsert_task_record,
)
from todo_system.trackers.linear import LinearAdapter

LINEAR_KEY = "lin_api_fixture-SUPERSECRET-linear"   # fixture secret — must never leak
PR_URL = "https://github.com/example/repo/pull/5"
ISSUE_UUID = "issue-uuid-0001"

LINEAR_ENV = ("MACP_LINEAR_TEAM", "MACP_LINEAR_API_URL", "MACP_LINEAR_API_KEY")


# --------------------------------------------------------------------------- #
# Recording server (GraphQL)
# --------------------------------------------------------------------------- #


def _lookup(**kw) -> dict:
    issue = {
        "id": ISSUE_UUID,
        "identifier": "ENG-123",
        "team": {"states": {"nodes": [
            {"id": "state-started", "name": "In Progress", "type": "started", "position": 2},
            {"id": "state-done-b", "name": "Shipped", "type": "completed", "position": 3},
            {"id": "state-done-a", "name": "Done", "type": "completed", "position": 1},
            {"id": "state-canceled", "name": "Canceled", "type": "canceled", "position": 4},
        ]}},
    }
    issue.update(kw)
    return issue


class RecordingLinearServer:
    """A local Linear GraphQL endpoint: records every request and replays scripted
    responses keyed on the request's ``operationName``."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.issues: list[dict] = []
        self.issue_lookup: dict | None = _lookup()
        self.update_success = True
        self.comment_success = True
        self.graphql_errors: list | None = None
        self.fail_code: int | None = None
        self.raw_body: bytes | None = None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence stderr noise
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = None
                server.requests.append({
                    "method": "POST",
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                    "user_agent": self.headers.get("User-Agent"),
                    "json": body,
                })
                code, data = server._response_for(body)
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _not_allowed(self):
                self.send_response(405)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_PATCH = do_PUT = do_DELETE = _not_allowed

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def _response_for(self, body: object) -> tuple[int, bytes]:
        if self.fail_code is not None:
            return self.fail_code, json.dumps({"message": "scripted failure"}).encode()
        if self.raw_body is not None:
            return 200, self.raw_body
        if self.graphql_errors is not None:
            return 200, json.dumps({"data": None, "errors": self.graphql_errors}).encode()
        op = body.get("operationName") if isinstance(body, dict) else None
        if op == "MacpPullIssues":
            data = {"issues": {"nodes": self.issues}}
        elif op == "MacpIssueForPush":
            data = {"issue": self.issue_lookup}
        elif op == "MacpIssueComplete":
            data = {"issueUpdate": {"success": self.update_success}}
        elif op == "MacpCommentCreate":
            data = {"commentCreate": {"success": self.comment_success}}
        else:
            return 400, json.dumps({"errors": [{"extensions": {"code": "BAD_OP"}}]}).encode()
        return 200, json.dumps({"data": data}).encode()

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/graphql"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def server():
    srv = RecordingLinearServer()
    yield srv
    srv.close()


def _adapter(server: RecordingLinearServer) -> LinearAdapter:
    return LinearAdapter("ENG", LINEAR_KEY, api_url=server.api_url)


def _issue(**kw) -> dict:
    issue = {
        "id": ISSUE_UUID,
        "identifier": "ENG-123",
        "title": "Fix the login bug",
        "description": "Users cannot log in.\n\n- [ ] repro on **staging**",
        "priority": 2,
        "dueDate": "2026-11-01",
        "updatedAt": "2026-10-20T12:00:00.000Z",
        "state": {"name": "In Progress", "type": "started"},
        "labels": {"nodes": [{"name": "backend"}]},
    }
    issue.update(kw)
    return issue


def _ops(server: RecordingLinearServer) -> list[str]:
    return [r["json"]["operationName"] for r in server.requests]


# --------------------------------------------------------------------------- #
# Linear: pull
# --------------------------------------------------------------------------- #


def test_pull_maps_fields_and_request_shape(server):
    server.issues = [_issue()]
    records = _adapter(server).pull()

    assert len(records) == 1
    rec = records[0]
    assert rec.external_ref == "linear:ENG-123"
    assert rec.title == "Fix the login bug"
    assert rec.body == "Users cannot log in.\n\n- [ ] repro on **staging**"  # Markdown verbatim
    assert rec.status == "open"
    assert rec.frontmatter == {"tags": ["backend"], "due": "2026-11-01", "priority": 70}
    assert rec.remote_updated_at == "2026-10-20T12:00:00.000Z"

    assert len(server.requests) == 1
    req = server.requests[0]
    assert req["method"] == "POST"
    assert req["path"] == "/graphql"
    assert req["authorization"] == LINEAR_KEY          # personal API key, no prefix
    assert req["content_type"] == "application/json"
    assert req["user_agent"]
    assert req["json"]["operationName"] == "MacpPullIssues"
    assert req["json"]["variables"] == {"filter": {"team": {"key": {"eq": "ENG"}}}, "first": 100}
    assert "query MacpPullIssues" in req["json"]["query"]
    assert req["json"]["query"] == linear.PULL_QUERY


def test_pull_upsert_creates_md_and_is_idempotent(server, tmp_path):
    server.issues = [_issue()]
    items = tmp_path / "items"
    adapter = _adapter(server)

    first = upsert_task_record(adapter.pull()[0], items, today="2026-10-25")
    assert first.action == "created"
    assert first.path.name == "fix-the-login-bug.md"
    before = first.path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(before)
    assert fm["external_ref"] == "linear:ENG-123"
    assert fm["external_updated_at"] == "2026-10-20T12:00:00.000Z"
    assert fm["due"] == "2026-11-01" and fm["priority"] == "70"
    assert "backend" in fm["tags"]
    assert "# Fix the login bug" in body and "repro on **staging**" in body

    second = upsert_task_record(adapter.pull()[0], items, today="2026-10-26")
    assert second.action == "unchanged"
    assert second.path == first.path
    assert sorted(p.name for p in items.glob("*.md")) == ["fix-the-login-bug.md"]
    assert second.path.read_text(encoding="utf-8") == before   # byte-identical


def test_pull_untitled_issue_falls_back_to_ref_slug(server, tmp_path):
    server.issues = [_issue(title="")]
    rec = _adapter(server).pull()[0]
    assert rec.title == "linear:ENG-123"
    result = upsert_task_record(rec, tmp_path / "items", today="2026-10-25")
    assert result.path.name == "linear-eng-123.md"


def test_pull_since_bare_date_and_iso(server):
    adapter = _adapter(server)
    adapter.pull(since="2026-10-01")
    adapter.pull(since="2026-10-01T09:30:00Z")

    f0 = server.requests[0]["json"]["variables"]["filter"]
    f1 = server.requests[1]["json"]["variables"]["filter"]
    assert f0 == {"team": {"key": {"eq": "ENG"}}, "updatedAt": {"gte": "2026-10-01T00:00:00Z"}}
    assert f1["updatedAt"] == {"gte": "2026-10-01T09:30:00Z"}


@pytest.mark.parametrize("state_type,status,cancelled", [
    ("backlog", "open", False),
    ("unstarted", "open", False),
    ("started", "open", False),
    ("triage", "open", False),
    ("completed", "done", False),
    ("canceled", "done", True),
])
def test_state_type_mapping(server, state_type, status, cancelled):
    server.issues = [_issue(state={"name": "whatever", "type": state_type})]
    rec = _adapter(server).pull()[0]
    assert rec.status == status
    if cancelled:
        assert rec.frontmatter["tags"] == ["backend", "cancelled"]
    else:
        assert "cancelled" not in rec.frontmatter["tags"]


def test_canceled_without_labels_gets_cancelled_tag(server):
    server.issues = [_issue(state={"name": "Canceled", "type": "canceled"}, labels=None)]
    assert _adapter(server).pull()[0].frontmatter["tags"] == ["cancelled"]


@pytest.mark.parametrize("prio,expected", [
    (0, None), (1, 90), (2, 70), (3, 50), (4, 30), (None, None), (5, None), (True, None),
])
def test_priority_mapping(prio, expected):
    adapter = LinearAdapter("ENG", "k", api_url="http://127.0.0.1:1/graphql")
    mapped = adapter.map_fields({"priority": prio})
    assert mapped.get("priority") == expected
    if expected is None:
        assert "priority" not in mapped


def test_map_fields_tolerates_missing_and_null_fields():
    adapter = LinearAdapter("ENG", "k", api_url="http://127.0.0.1:1/graphql")
    assert adapter.map_fields({}) == {}
    assert adapter.map_fields({"labels": None, "dueDate": None}) == {}
    assert adapter.map_fields({"labels": {"nodes": None}}) == {}
    assert adapter.map_fields(
        {"labels": {"nodes": [{"name": " ui "}, {"name": ""}, "x", {}]}}) == {"tags": ["ui"]}
    assert adapter.map_fields({"dueDate": "2026-12-24T00:00:00Z"}) == {"due": "2026-12-24"}


def test_pull_skips_nodes_without_identifier(server):
    server.issues = [_issue(identifier=None), _issue(identifier=""), "junk", _issue()]
    records = _adapter(server).pull()
    assert [r.external_ref for r in records] == ["linear:ENG-123"]


def test_graphql_errors_raise_with_code_only(server):
    server.graphql_errors = [
        {"message": f"bad key {LINEAR_KEY}", "extensions": {"code": "AUTHENTICATION_ERROR"}},
        {"message": "second", "extensions": {"code": "AUTHENTICATION_ERROR"}},
    ]
    with pytest.raises(TrackerError) as excinfo:
        _adapter(server).pull()
    msg = str(excinfo.value)
    assert "MacpPullIssues" in msg and "200" in msg and "AUTHENTICATION_ERROR" in msg
    assert msg.count("AUTHENTICATION_ERROR") == 1
    assert LINEAR_KEY not in msg
    assert "bad key" not in msg


def test_graphql_errors_without_code_read_unknown(server):
    server.graphql_errors = [{"message": "boom"}]
    with pytest.raises(TrackerError, match=r"GraphQL error \(unknown\)"):
        _adapter(server).pull()


def test_non_json_response_raises(server):
    server.raw_body = b"<html>"
    with pytest.raises(TrackerError, match="non-JSON response"):
        _adapter(server).pull()


def test_missing_data_raises_malformed(server):
    server.raw_body = b'{"data": null}'
    with pytest.raises(TrackerError, match="malformed response"):
        _adapter(server).pull()


# --------------------------------------------------------------------------- #
# Linear: push
# --------------------------------------------------------------------------- #


def test_push_done_with_pr_sends_lookup_update_comment(server):
    task = TaskRecord(external_ref="linear:ENG-123", status="done", pr_url=PR_URL)
    assert _adapter(server).push(task) == "linear:ENG-123"

    reqs = server.requests
    assert _ops(server) == ["MacpIssueForPush", "MacpIssueComplete", "MacpCommentCreate"]
    assert reqs[0]["json"]["variables"] == {"id": "ENG-123"}
    assert reqs[0]["json"]["query"] == linear.ISSUE_QUERY
    # the lowest-position `completed` state wins (state-done-a, position 1)
    assert reqs[1]["json"]["variables"] == {"id": ISSUE_UUID, "stateId": "state-done-a"}
    assert reqs[1]["json"]["query"] == linear.UPDATE_MUTATION
    assert reqs[2]["json"]["variables"] == {
        "issueId": ISSUE_UUID, "body": f"macp: PR merged — {PR_URL}"}
    assert reqs[2]["json"]["query"] == linear.COMMENT_MUTATION
    assert all(r["path"] == "/graphql" for r in reqs)
    assert all(r["authorization"] == LINEAR_KEY for r in reqs)


def test_push_done_without_pr_updates_only(server):
    _adapter(server).push(TaskRecord(external_ref="linear:ENG-123", status="done"))
    assert _ops(server) == ["MacpIssueForPush", "MacpIssueComplete"]


def test_push_pr_on_open_task_comments_only(server):
    _adapter(server).push(TaskRecord(external_ref="linear:ENG-123", status="open", pr_url=PR_URL))
    assert _ops(server) == ["MacpIssueForPush", "MacpCommentCreate"]


def test_push_no_completed_state_raises_before_update(server):
    server.issue_lookup = _lookup(team={"states": {"nodes": [
        {"id": "state-started", "name": "In Progress", "type": "started", "position": 1},
    ]}})
    with pytest.raises(TrackerError, match="no completed workflow state for ENG-123"):
        _adapter(server).push(
            TaskRecord(external_ref="linear:ENG-123", status="done", pr_url=PR_URL))
    assert _ops(server) == ["MacpIssueForPush"]


def test_push_update_success_false_raises(server):
    server.update_success = False
    with pytest.raises(TrackerError, match="MacpIssueComplete -> success false"):
        _adapter(server).push(
            TaskRecord(external_ref="linear:ENG-123", status="done", pr_url=PR_URL))
    assert _ops(server) == ["MacpIssueForPush", "MacpIssueComplete"]


def test_push_comment_success_false_raises(server):
    server.comment_success = False
    with pytest.raises(TrackerError, match="MacpCommentCreate -> success false"):
        _adapter(server).push(
            TaskRecord(external_ref="linear:ENG-123", status="open", pr_url=PR_URL))


def test_push_issue_not_found_raises(server):
    server.issue_lookup = None
    with pytest.raises(TrackerError, match="issue ENG-123 not found"):
        _adapter(server).push(TaskRecord(external_ref="linear:ENG-123", status="done"))
    assert _ops(server) == ["MacpIssueForPush"]


@pytest.mark.parametrize(
    "ref", ["jira:PROJ-1", "linear:ENG", "linear:ENG-x", "github:example/repo#7"])
def test_push_bad_ref_raises_without_request(server, ref):
    with pytest.raises(TrackerError) as excinfo:
        _adapter(server).push(TaskRecord(external_ref=ref, status="done", pr_url=PR_URL))
    assert ref in str(excinfo.value)
    assert server.requests == []


def test_push_bad_ref_with_nothing_to_send_is_noop(server):
    assert _adapter(server).push(TaskRecord(external_ref="jira:PROJ-1", status="open")) == (
        "jira:PROJ-1")
    assert server.requests == []


# --------------------------------------------------------------------------- #
# Linear: from_config opt-in gate
# --------------------------------------------------------------------------- #


def _clear_env(monkeypatch, names=LINEAR_ENV + ("LINEAR_API_KEY",)) -> None:
    for var in names:
        monkeypatch.delenv(var, raising=False)


def test_from_config_reads_config_and_secrets(server, tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text(
        f'linear_team = "ENG"\nlinear_api_url = "{server.api_url}"\n', encoding="utf-8")
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "linear.json").write_text(
        json.dumps({"api_key": LINEAR_KEY}), encoding="utf-8")

    adapter = LinearAdapter.from_config(vault_root=tmp_path, start=tmp_path)
    assert isinstance(adapter, LinearAdapter)
    assert adapter.team == "ENG" and adapter.api_url == server.api_url
    server.issues = [_issue()]
    assert len(adapter.pull()) == 1
    assert server.requests[0]["authorization"] == LINEAR_KEY


def test_from_config_env_overrides(server, tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACP_LINEAR_TEAM", "ENG")
    monkeypatch.setenv("MACP_LINEAR_API_URL", server.api_url)
    monkeypatch.setenv("MACP_LINEAR_API_KEY", LINEAR_KEY)
    adapter = LinearAdapter.from_config(vault_root=tmp_path, start=tmp_path)
    assert adapter is not None and adapter.api_url == server.api_url


def test_from_config_defaults_api_url(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACP_LINEAR_TEAM", "ENG")
    monkeypatch.setenv("MACP_LINEAR_API_KEY", LINEAR_KEY)
    adapter = LinearAdapter.from_config(vault_root=tmp_path, start=tmp_path)
    assert adapter is not None and adapter.api_url == "https://api.linear.app/graphql"


def test_from_config_none_without_team(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACP_LINEAR_API_KEY", LINEAR_KEY)
    assert LinearAdapter.from_config(vault_root=tmp_path, start=tmp_path) is None


def test_from_config_none_without_key(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACP_LINEAR_TEAM", "ENG")
    assert LinearAdapter.from_config(vault_root=tmp_path, start=tmp_path) is None


@pytest.mark.parametrize("team", ["ENG TEAM", "a/b"])
def test_from_config_rejects_malformed_team(tmp_path, monkeypatch, team):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACP_LINEAR_TEAM", team)
    monkeypatch.setenv("MACP_LINEAR_API_KEY", LINEAR_KEY)
    assert LinearAdapter.from_config(vault_root=tmp_path, start=tmp_path) is None


def test_from_config_ignores_ambient_linear_api_key(server, tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACP_LINEAR_TEAM", "ENG")
    monkeypatch.setenv("LINEAR_API_KEY", LINEAR_KEY)   # ambient key must not switch sync on
    assert LinearAdapter.from_config(vault_root=tmp_path, start=tmp_path) is None
    assert server.requests == []


def test_load_linear_api_key_bad_secrets_file_reads_as_unset(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "linear.json").write_text("[1, 2]", encoding="utf-8")
    assert base.load_linear_api_key(tmp_path) is None
    (tmp_path / ".secrets" / "linear.json").write_text("{not json", encoding="utf-8")
    assert base.load_linear_api_key(tmp_path) is None


def test_linear_tracker_error_is_shared():
    assert linear.TrackerError is base.TrackerError


# --------------------------------------------------------------------------- #
# Tracker contract — mirrored on Linear (same names/assertions as the Jira/GitHub set)
# --------------------------------------------------------------------------- #


@dataclass
class Case:
    name: str
    server: RecordingLinearServer
    adapter: TrackerAdapter
    token: str
    seed: Callable[[], None]
    ref_prefix: str
    done_ref: str
    env_vars: tuple[str, ...]
    from_config: Callable[..., object]

    def auth_carries_token(self, auth: str) -> bool:
        # Linear personal API keys travel unprefixed; accept a Bearer form too.
        return auth == self.token or auth == f"Bearer {self.token}"


@pytest.fixture(params=["linear"])
def contract(request, server) -> Case:
    def seed() -> None:
        server.issues = [_issue()]

    return Case(
        name="linear", server=server, adapter=_adapter(server), token=LINEAR_KEY,
        seed=seed, ref_prefix="linear:", done_ref="linear:ENG-123",
        env_vars=LINEAR_ENV, from_config=LinearAdapter.from_config,
    )


def test_contract_is_tracker_adapter(contract):
    assert isinstance(contract.adapter, TrackerAdapter)


def test_contract_pull_upsert_idempotent(contract, tmp_path):
    contract.seed()
    items = tmp_path / "items"
    first = [upsert_task_record(r, items, today="2026-10-25") for r in contract.adapter.pull()]
    second = [upsert_task_record(r, items, today="2026-10-26") for r in contract.adapter.pull()]

    assert [r.action for r in first] == ["created"]
    assert [r.action for r in second] == ["unchanged"]
    assert len(list(items.glob("*.md"))) == 1
    fm, _ = parse_frontmatter(first[0].path.read_text(encoding="utf-8"))
    assert str(fm["external_ref"]).startswith(contract.ref_prefix)


def test_contract_push_open_without_pr_is_noop(contract):
    ref = contract.adapter.push(TaskRecord(external_ref=contract.done_ref, status="open"))
    assert ref == contract.done_ref
    assert contract.server.requests == []


def test_contract_push_done_with_pr_returns_ref_and_sends_pr_url(contract):
    task = TaskRecord(external_ref=contract.done_ref, status="done", pr_url=PR_URL)
    assert contract.adapter.push(task) == contract.done_ref
    assert any(PR_URL in json.dumps(r["json"]) for r in contract.server.requests if r["json"])


def test_contract_from_config_none_without_config(contract, tmp_path, monkeypatch):
    for var in contract.env_vars + ("LINEAR_API_KEY",):
        monkeypatch.delenv(var, raising=False)
    assert contract.from_config(vault_root=tmp_path, start=tmp_path) is None
    assert contract.server.requests == []
    assert not list(tmp_path.glob("**/*.md"))


def test_contract_token_absent_from_md_and_output(contract, tmp_path, capsys):
    contract.seed()
    result = upsert_task_record(contract.adapter.pull()[0], tmp_path / "items", today="2026-10-25")
    md_text = result.path.read_text(encoding="utf-8")
    captured = capsys.readouterr()

    assert contract.token not in md_text
    assert contract.token not in (captured.out + captured.err)
    # The server DID receive the token (auth really sent, not dropped).
    assert contract.auth_carries_token(contract.server.requests[0]["authorization"])


def test_contract_http_error_hides_token(contract):
    contract.server.fail_code = 401
    with pytest.raises(TrackerError) as excinfo:
        contract.adapter.pull()
    msg = str(excinfo.value)
    assert "401" in msg
    assert contract.token not in msg
    assert "Basic" not in msg and "Bearer" not in msg


# --------------------------------------------------------------------------- #
# Import is transport-free (kept last: it reloads the adapter module)
# --------------------------------------------------------------------------- #


def test_import_and_none_config_make_no_network_call(tmp_path, monkeypatch):
    def _no_network(*args, **kwargs):
        raise AssertionError("network call during import / unconfigured from_config")

    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    _clear_env(monkeypatch)
    import todo_system.trackers.linear as linear_mod

    linear_mod = importlib.reload(linear_mod)
    assert linear_mod.LinearAdapter.from_config(vault_root=tmp_path, start=tmp_path) is None
    assert not list(Path(tmp_path).glob("**/*.md"))
