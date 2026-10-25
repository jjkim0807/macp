"""Tests for the TrackerAdapter contract + Jira adapter (design §⑨).

Two layers, both offline:

* **md bridge** (``base.upsert_task_record``) — idempotent upsert, the monotonic
  "done 승격" status rule, and the md-wins conflict note, driven against a
  ``tmp_path`` items dir (never the real vault — the session-scoped
  ``_real_vault_untouched`` conftest guard backs this up).
* **Jira transport** (``jira.JiraAdapter``) — exercised against a local
  ``RecordingJiraServer`` (``127.0.0.1:0``, mirrors ``test_slack_send.py``) so
  pull/push round-trips, the opt-in no-op, and token secrecy are asserted on the
  exact bytes without ever reaching a real Jira site.
* **Tracker contract** — the shared ``_tracker_contract.py`` assertions, run here as
  ``[jira]`` against ``RecordingJiraServer`` (loaded by path, import-mode independent).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from todo_system.trackers.base import (
    TaskRecord,
    append_progress_log,
    parse_frontmatter,
    upsert_task_record,
)
from todo_system.trackers.jira import JiraAdapter, TrackerError


def _load_contract():
    name = "macp_tracker_contract"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, Path(__file__).with_name("_tracker_contract.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[name]


_contract = _load_contract()


# --------------------------------------------------------------------------- #
# md bridge: upsert / idempotency / conflict / monotonic status (no network)
# --------------------------------------------------------------------------- #


def _record(**kw) -> TaskRecord:
    base = dict(
        external_ref="jira:PROJ-123",
        title="Fix the thing",
        body="Do the work.",
        frontmatter={"tags": ["backend"], "due": "2026-10-10", "priority": 70},
        status="open",
        remote_updated_at="2026-10-01T00:00:00.000+0000",
    )
    base.update(kw)
    return TaskRecord(**base)


def test_pull_upsert_creates_md(tmp_path):
    items = tmp_path / "items"
    result = upsert_task_record(_record(), items, today="2026-10-24")

    assert result.action == "created"
    assert result.path.exists()
    text = result.path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    assert fm["external_ref"] == "jira:PROJ-123"
    assert fm["external_updated_at"] == "2026-10-01T00:00:00.000+0000"
    assert "todo" in fm["tags"] and "backend" in fm["tags"]
    assert fm["due"] == "2026-10-10"
    assert "# Fix the thing" in body
    assert "Do the work." in body


def test_pull_idempotent_no_duplicate(tmp_path):
    items = tmp_path / "items"
    first = upsert_task_record(_record(), items, today="2026-10-24")
    before = first.path.read_text(encoding="utf-8")

    second = upsert_task_record(_record(), items, today="2026-10-25")  # same remote, later day

    assert second.action == "unchanged"
    assert second.path == first.path
    assert sorted(p.name for p in items.glob("*.md")) == [first.path.name]
    assert second.path.read_text(encoding="utf-8") == before  # byte-identical, no churn


def test_conflict_keeps_md_and_notes_human(tmp_path):
    items = tmp_path / "items"
    created = upsert_task_record(_record(), items, today="2026-10-24")

    # Human edits the md's due (md-side change).
    human = created.path.read_text(encoding="utf-8").replace("due: 2026-10-10", "due: 2026-10-20")
    created.path.write_text(human, encoding="utf-8")

    # Remote also changed due AND advanced its updated timestamp (remote-side change).
    diverged = _record(
        frontmatter={"tags": ["backend"], "due": "2026-10-15", "priority": 70},
        remote_updated_at="2026-10-05T00:00:00.000+0000",
    )
    result = upsert_task_record(diverged, items, today="2026-10-26")

    assert result.action == "conflict"
    assert result.conflicts == ["due"]
    text = result.path.read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(text)
    assert fm["due"] == "2026-10-20"              # md wins — not overwritten
    assert "트래커 충돌: due" in text               # human-readable trace
    assert "2026-10-15" in text                    # names the tracker value
    assert text.index("## Progress Log") < text.index("트래커 충돌")  # note is in the log


def test_conflict_silent_when_remote_not_advanced(tmp_path):
    """md differs from a STALE remote (not advanced): md wins silently, no note."""
    items = tmp_path / "items"
    created = upsert_task_record(_record(), items, today="2026-10-24")
    human = created.path.read_text(encoding="utf-8").replace("due: 2026-10-10", "due: 2026-10-20")
    created.path.write_text(human, encoding="utf-8")

    # Same remote timestamp as last sync → remote did NOT advance.
    result = upsert_task_record(_record(frontmatter={"due": "2026-10-15"}), items, today="2026-10-26")

    assert result.action == "unchanged"
    assert "트래커 충돌" not in result.path.read_text(encoding="utf-8")


def test_status_monotonic_done_sticky(tmp_path):
    """md is done; a remote that still reads open must NOT reopen it (§⑨)."""
    items = tmp_path / "items"
    created = upsert_task_record(_record(), items, today="2026-10-24")
    closed = created.path.read_text(encoding="utf-8").replace("status: open", "status: done")
    created.path.write_text(closed, encoding="utf-8")

    result = upsert_task_record(_record(status="open"), items, today="2026-10-25")

    fm, _ = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert fm["status"] == "done"  # stayed done — reopen only from md


def test_status_promoted_to_done_from_remote(tmp_path):
    """md is open; a remote that reads done promotes it to done (§⑨)."""
    items = tmp_path / "items"
    upsert_task_record(_record(), items, today="2026-10-24")

    result = upsert_task_record(
        _record(status="done", remote_updated_at="2026-10-05T00:00:00.000+0000"),
        items,
        today="2026-10-25",
    )

    fm, _ = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert fm["status"] == "done"


def test_upsert_fills_empty_md_field(tmp_path):
    """An empty md-owned field is filled from the remote (not a conflict)."""
    items = tmp_path / "items"
    created = upsert_task_record(_record(frontmatter={"tags": ["backend"]}), items, today="2026-10-24")
    fm, _ = parse_frontmatter(created.path.read_text(encoding="utf-8"))
    assert fm.get("due") in (None, "-")  # nothing to seed it

    result = upsert_task_record(
        _record(frontmatter={"tags": ["backend"], "due": "2026-11-01"},
                remote_updated_at="2026-10-05T00:00:00.000+0000"),
        items,
        today="2026-10-25",
    )
    fm2, _ = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert fm2["due"] == "2026-11-01"
    assert result.conflicts == []


def test_append_progress_log_creates_section_when_missing(tmp_path):
    md = "---\nstatus: open\n---\n\n# No log here\n\nbody\n"
    out = append_progress_log(md, "first entry", today="2026-10-24")
    assert "## Progress Log" in out
    assert "- 2026-10-24: first entry" in out


# --------------------------------------------------------------------------- #
# Jira transport: pull/push round-trips against a local recording server
# --------------------------------------------------------------------------- #

TOKEN = "atlassian-tok-SUPERSECRET-xyz"  # the fixture secret — must never leak


class RecordingJiraServer:
    """A local Jira endpoint: records every request (path/method/auth/JSON body)
    and replays scripted responses. Mirrors ``test_slack_send.RecordingSlackServer``
    so the exact request bytes can be asserted without a real Jira site."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.search_issues: list[dict] = []
        self.search_code = 200
        self.transitions = {"transitions": [
            {"id": "11", "name": "Start", "to": {"name": "In Progress"}},
            {"id": "31", "name": "Mark done", "to": {"name": "Done"}},
        ]}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence stderr noise
                pass

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = None
                server.requests.append({
                    "method": method,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "json": body,
                })
                code, payload = server._response_for(method, self.path)
                if code == 204:
                    self.send_response(204)
                    self.end_headers()
                    return
                data = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def _response_for(self, method: str, path: str) -> tuple[int, dict]:
        if method == "GET" and "/search" in path:
            return self.search_code, {"issues": self.search_issues}
        if method == "GET" and path.endswith("/transitions"):
            return 200, self.transitions
        if method == "POST" and path.endswith("/transitions"):
            return 204, {}
        if method == "POST" and path.endswith("/comment"):
            return 201, {"id": "10000"}
        return 200, {}

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


@pytest.fixture
def jira_server():
    srv = RecordingJiraServer()
    yield srv
    srv.close()


def _adapter(server: RecordingJiraServer) -> JiraAdapter:
    return JiraAdapter(
        base_url=server.base_url, project="PROJ",
        email="dev@example.test", api_token=TOKEN,
    )


def _issue_fixture(**fields) -> dict:
    base = {
        "summary": "Fix the login bug",
        "description": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Users cannot log in."}]}]},
        "status": {"name": "In Progress"},
        "labels": ["backend", "urgent"],
        "duedate": "2026-11-01",
        "priority": {"name": "High"},
        "updated": "2026-10-20T12:00:00.000+0000",
    }
    base.update(fields)
    return {"key": "PROJ-123", "fields": base}


def test_pull_roundtrip_then_upsert(jira_server, tmp_path):
    jira_server.search_issues = [_issue_fixture()]
    records = _adapter(jira_server).pull()

    assert len(records) == 1
    rec = records[0]
    assert rec.external_ref == "jira:PROJ-123"
    assert rec.title == "Fix the login bug"
    assert rec.body == "Users cannot log in."
    assert rec.status == "open"                       # In Progress → open
    assert rec.remote_updated_at == "2026-10-20T12:00:00.000+0000"
    assert rec.frontmatter["due"] == "2026-11-01"
    assert rec.frontmatter["priority"] == 70          # High → 70
    assert rec.frontmatter["tags"] == ["backend", "urgent"]

    result = upsert_task_record(rec, tmp_path / "items", today="2026-10-24")
    fm, _ = parse_frontmatter(result.path.read_text(encoding="utf-8"))
    assert fm["external_ref"] == "jira:PROJ-123"

    search = [r for r in jira_server.requests if "/search" in r["path"]]
    assert len(search) == 1 and search[0]["authorization"].startswith("Basic ")


def test_pull_cancelled_status_adds_cancelled_tag(jira_server):
    jira_server.search_issues = [_issue_fixture(status={"name": "Cancelled"}, labels=["backend"])]
    rec = _adapter(jira_server).pull()[0]
    assert rec.status == "done"                       # Cancelled → done
    assert "cancelled" in rec.frontmatter["tags"]


def test_push_transition_and_comment_bodies_match_mapping(jira_server):
    task = TaskRecord(
        external_ref="jira:PROJ-123", status="done",
        pr_url="https://github.com/example/repo/pull/5",
    )
    assert _adapter(jira_server).push(task) == "jira:PROJ-123"

    reqs = jira_server.requests
    get_trans = [r for r in reqs if r["method"] == "GET" and r["path"].endswith("/transitions")]
    post_trans = [r for r in reqs if r["method"] == "POST" and r["path"].endswith("/transitions")]
    comment = [r for r in reqs if r["method"] == "POST" and r["path"].endswith("/comment")]

    assert len(get_trans) == 1
    assert "/rest/api/3/issue/PROJ-123/transitions" in get_trans[0]["path"]
    assert post_trans and post_trans[0]["json"] == {"transition": {"id": "31"}}  # the "Done" id
    assert comment and comment[0]["json"]["body"]["type"] == "doc"               # minimal ADF
    text_nodes = comment[0]["json"]["body"]["content"][0]["content"]
    assert any("github.com/example/repo/pull/5" in n.get("text", "") for n in text_nodes)


def test_push_open_task_without_pr_is_noop(jira_server):
    _adapter(jira_server).push(TaskRecord(external_ref="jira:PROJ-9", status="open"))
    assert jira_server.requests == []                 # not done, no PR → nothing sent


def test_optin_noop_without_config(jira_server, tmp_path, monkeypatch):
    for var in ("MACP_JIRA_BASE_URL", "MACP_JIRA_PROJECT", "MACP_JIRA_EMAIL", "MACP_JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    adapter = JiraAdapter.from_config(vault_root=tmp_path, start=tmp_path)

    assert adapter is None                            # no config → no adapter
    assert jira_server.requests == []                 # zero network calls
    assert not list(tmp_path.glob("**/*.md"))         # no md created


def test_token_absent_from_md_and_stdout(jira_server, tmp_path, capsys):
    jira_server.search_issues = [_issue_fixture()]
    adapter = _adapter(jira_server)
    records = adapter.pull()
    result = upsert_task_record(records[0], tmp_path / "items", today="2026-10-24")
    md_text = result.path.read_text(encoding="utf-8")
    captured = capsys.readouterr()

    assert TOKEN not in md_text
    assert TOKEN not in (captured.out + captured.err)
    # The server DID receive the token in a Basic header (auth really sent, not dropped).
    auth = next(r["authorization"] for r in jira_server.requests if "/search" in r["path"])
    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
    assert decoded == f"dev@example.test:{TOKEN}"


def test_error_message_carries_no_token(jira_server):
    jira_server.search_code = 401
    with pytest.raises(TrackerError) as excinfo:
        _adapter(jira_server).pull()
    msg = str(excinfo.value)
    assert "401" in msg
    assert TOKEN not in msg and "Basic" not in msg


def test_map_fields_maps_priority_labels_due():
    adapter = JiraAdapter(base_url="http://127.0.0.1:1", project="PROJ",
                          email="e@x.test", api_token="t")
    mapped = adapter.map_fields(
        {"labels": ["a", "b"], "duedate": "2026-12-01", "priority": {"name": "Lowest"}})
    assert mapped == {"tags": ["a", "b"], "due": "2026-12-01", "priority": 10}
    assert adapter.map_fields({}) == {}               # nothing set when fields absent


# --------------------------------------------------------------------------- #
# Tracker contract — shared assertions (_tracker_contract.py) on Jira
# --------------------------------------------------------------------------- #

JIRA_ENV = ("MACP_JIRA_BASE_URL", "MACP_JIRA_PROJECT", "MACP_JIRA_EMAIL", "MACP_JIRA_API_TOKEN")


@pytest.fixture(params=["jira"])
def contract(request, jira_server):
    def seed() -> None:
        jira_server.search_issues = [_issue_fixture()]

    return _contract.Case(
        name="jira", server=jira_server, adapter=_adapter(jira_server), token=TOKEN,
        seed=seed, fail=lambda code: setattr(jira_server, "search_code", code),
        ref_prefix="jira:", done_ref="jira:PROJ-123",
        env_vars=JIRA_ENV + ("GITHUB_TOKEN",), from_config=JiraAdapter.from_config,
        auth_carries_token=_contract.bearer_or_basic(TOKEN),
    )


test_contract_is_tracker_adapter = _contract.test_contract_is_tracker_adapter
test_contract_pull_upsert_idempotent = _contract.test_contract_pull_upsert_idempotent
test_contract_push_open_without_pr_is_noop = _contract.test_contract_push_open_without_pr_is_noop
test_contract_push_done_with_pr_returns_ref_and_sends_pr_url = (
    _contract.test_contract_push_done_with_pr_returns_ref_and_sends_pr_url)
test_contract_from_config_none_without_config = _contract.test_contract_from_config_none_without_config
test_contract_token_absent_from_md_and_output = _contract.test_contract_token_absent_from_md_and_output
test_contract_http_error_hides_token = _contract.test_contract_http_error_hides_token
