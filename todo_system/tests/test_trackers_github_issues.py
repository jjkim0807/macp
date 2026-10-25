"""Tests for the GitHub Issues adapter + the Jira/GitHub tracker contract (design §⑨).

Two layers, both offline against one local ``RecordingTrackerServer``
(``127.0.0.1:0``, the ``test_trackers_jira.RecordingJiraServer`` pattern plus
``PATCH``), which serves the GitHub Issues routes **and** the Jira routes:

* **GitHub transport** (``github_issues.GitHubIssuesAdapter``) — pull mapping and PR
  filtering, idempotent upsert, push close/comment request bodies, the opt-in
  ``from_config`` gate, and token secrecy, asserted on the exact request bytes.
* **Tracker contract** — the behaviour every :class:`TrackerAdapter` owes the md
  bridge, parameterized over ``[jira]`` and ``[github]``. Self-contained on purpose:
  ``todo_system/tests`` is not a package, so importing ``test_trackers_jira``'s server
  would depend on pytest's import mode.

Everything runs against ``tmp_path`` — never the real vault (the session-scoped
``_real_vault_untouched`` conftest guard backs this up).
"""

from __future__ import annotations

import base64
import importlib
import json
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable

import pytest

from todo_system.trackers import base, jira
from todo_system.trackers.base import (
    TaskRecord,
    TrackerAdapter,
    TrackerError,
    parse_frontmatter,
    upsert_task_record,
)
from todo_system.trackers.github_issues import GitHubIssuesAdapter
from todo_system.trackers.jira import JiraAdapter

GH_TOKEN = "ghp-fixture-SUPERSECRET-gh"            # fixture secrets — must never leak
JIRA_TOKEN = "atlassian-fixture-SUPERSECRET-jira"
PR_URL = "https://github.com/example/repo/pull/5"

GH_ENV = ("MACP_GITHUB_ISSUES_REPO", "MACP_GITHUB_API_URL", "MACP_GITHUB_TOKEN")
JIRA_ENV = ("MACP_JIRA_BASE_URL", "MACP_JIRA_PROJECT", "MACP_JIRA_EMAIL", "MACP_JIRA_API_TOKEN")


# --------------------------------------------------------------------------- #
# Recording server (GitHub + Jira routes)
# --------------------------------------------------------------------------- #


class RecordingTrackerServer:
    """A local tracker endpoint: records every request and replays scripted
    responses for both the GitHub Issues and the Jira REST routes."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.gh_issues: list[dict] = []
        self.jira_issues: list[dict] = []
        self.fail_code: int | None = None
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
                    "accept": self.headers.get("Accept"),
                    "api_version": self.headers.get("X-GitHub-Api-Version"),
                    "user_agent": self.headers.get("User-Agent"),
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

            def do_PATCH(self):
                self._handle("PATCH")

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def _response_for(self, method: str, path: str) -> tuple[int, object]:
        if self.fail_code is not None:
            return self.fail_code, {"message": "scripted failure"}
        route = path.split("?", 1)[0]
        if route.startswith("/repos/"):
            if method == "GET" and route.endswith("/issues"):
                return 200, self.gh_issues
            if method == "PATCH" and "/issues/" in route:
                return 200, {}
            if method == "POST" and route.endswith("/comments"):
                return 201, {"id": 1}
            return 200, {}
        if method == "GET" and "/search" in route:
            return 200, {"issues": self.jira_issues}
        if method == "GET" and route.endswith("/transitions"):
            return 200, self.transitions
        if method == "POST" and route.endswith("/transitions"):
            return 204, {}
        if method == "POST" and route.endswith("/comment"):
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
def server():
    srv = RecordingTrackerServer()
    yield srv
    srv.close()


def _gh_adapter(server: RecordingTrackerServer) -> GitHubIssuesAdapter:
    return GitHubIssuesAdapter("example/repo", GH_TOKEN, base_url=server.base_url)


def _jira_adapter(server: RecordingTrackerServer) -> JiraAdapter:
    return JiraAdapter(
        base_url=server.base_url, project="PROJ",
        email="dev@example.test", api_token=JIRA_TOKEN,
    )


def _gh_issue(**kw) -> dict:
    issue = {
        "number": 7,
        "title": "Fix the login bug",
        "body": "Users cannot log in.\n\n- [ ] repro on **staging**",
        "state": "open",
        "state_reason": None,
        "labels": [{"name": "backend"}, {"name": "priority:high"}],
        "milestone": {"title": "v1", "due_on": "2026-11-01T07:00:00Z"},
        "updated_at": "2026-10-20T12:00:00Z",
    }
    issue.update(kw)
    return issue


def _gh_pr_item() -> dict:
    return _gh_issue(number=8, title="A pull request", pull_request={"url": "x"})


def _jira_issue() -> dict:
    return {"key": "PROJ-123", "fields": {
        "summary": "Fix the login bug",
        "description": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Users cannot log in."}]}]},
        "status": {"name": "In Progress"},
        "labels": ["backend"],
        "duedate": "2026-11-01",
        "priority": {"name": "High"},
        "updated": "2026-10-20T12:00:00.000+0000",
    }}


def _query(path: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)


# --------------------------------------------------------------------------- #
# GitHub: pull
# --------------------------------------------------------------------------- #


def test_pull_maps_fields_and_filters_prs(server):
    server.gh_issues = [_gh_issue(), _gh_pr_item()]
    records = _gh_adapter(server).pull()

    assert len(records) == 1                          # the PR item is filtered out
    rec = records[0]
    assert rec.external_ref == "github:example/repo#7"
    assert rec.title == "Fix the login bug"
    assert rec.body == "Users cannot log in.\n\n- [ ] repro on **staging**"  # Markdown verbatim
    assert rec.status == "open"
    assert rec.frontmatter["due"] == "2026-11-01"      # milestone.due_on date part
    assert rec.frontmatter["priority"] == 70           # priority:high → 70
    assert rec.frontmatter["tags"] == ["backend", "priority:high"]  # priority label kept
    assert rec.remote_updated_at == "2026-10-20T12:00:00Z"

    assert len(server.requests) == 1
    req = server.requests[0]
    assert req["method"] == "GET"
    assert req["path"].startswith("/repos/example/repo/issues?")
    q = _query(req["path"])
    assert q["state"] == ["all"] and q["per_page"] == ["100"]
    assert "since" not in q
    assert req["authorization"] == f"Bearer {GH_TOKEN}"
    assert req["accept"] == "application/vnd.github+json"
    assert req["api_version"] == "2022-11-28"
    assert req["user_agent"]


def test_pull_upsert_creates_md_and_is_idempotent(server, tmp_path):
    server.gh_issues = [_gh_issue()]
    items = tmp_path / "items"
    adapter = _gh_adapter(server)

    first = upsert_task_record(adapter.pull()[0], items, today="2026-10-25")
    assert first.action == "created"
    assert first.path.name == "fix-the-login-bug.md"
    before = first.path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(before)
    assert fm["external_ref"] == "github:example/repo#7"
    assert fm["external_updated_at"] == "2026-10-20T12:00:00Z"
    assert fm["due"] == "2026-11-01" and fm["priority"] == "70"
    assert "# Fix the login bug" in body and "repro on **staging**" in body

    second = upsert_task_record(adapter.pull()[0], items, today="2026-10-26")
    assert second.action == "unchanged"
    assert second.path == first.path
    assert sorted(p.name for p in items.glob("*.md")) == ["fix-the-login-bug.md"]
    assert second.path.read_text(encoding="utf-8") == before   # byte-identical


def test_pull_untitled_issue_falls_back_to_ref_slug(server, tmp_path):
    server.gh_issues = [_gh_issue(title="")]
    rec = _gh_adapter(server).pull()[0]
    assert rec.title == "github:example/repo#7"
    result = upsert_task_record(rec, tmp_path / "items", today="2026-10-25")
    assert result.path.name == "github-example-repo-7.md"


def test_pull_since_passes_iso_timestamp(server):
    adapter = _gh_adapter(server)
    adapter.pull(since="2026-10-01")
    adapter.pull(since="2026-10-01T09:30:00Z")

    assert _query(server.requests[0]["path"])["since"] == ["2026-10-01T00:00:00Z"]
    assert "since=2026-10-01T00%3A00%3A00Z" in server.requests[0]["path"]
    assert _query(server.requests[1]["path"])["since"] == ["2026-10-01T09:30:00Z"]


def test_closed_completed_maps_done(server):
    server.gh_issues = [_gh_issue(state="closed", state_reason="completed")]
    rec = _gh_adapter(server).pull()[0]
    assert rec.status == "done"
    assert "cancelled" not in rec.frontmatter["tags"]


def test_closed_without_reason_maps_done(server):
    server.gh_issues = [_gh_issue(state="closed", state_reason=None, labels=[])]
    rec = _gh_adapter(server).pull()[0]
    assert rec.status == "done"
    assert "tags" not in rec.frontmatter


@pytest.mark.parametrize("reason", ["not_planned", "duplicate"])
def test_closed_not_planned_maps_done_plus_cancelled(server, reason):
    server.gh_issues = [_gh_issue(state="closed", state_reason=reason)]
    rec = _gh_adapter(server).pull()[0]
    assert rec.status == "done"
    assert rec.frontmatter["tags"] == ["backend", "priority:high", "cancelled"]


def test_p_label_priority_and_max_wins():
    adapter = GitHubIssuesAdapter("example/repo", "t", base_url="http://127.0.0.1:1")
    mapped = adapter.map_fields({"labels": [{"name": "P3"}, {"name": "priority:highest"}]})
    assert mapped == {"tags": ["P3", "priority:highest"], "priority": 90}
    assert "due" not in mapped                          # no milestone → no due

    assert adapter.map_fields({"labels": ["Priority/Low", "p4"]})["priority"] == 30
    assert adapter.map_fields({"labels": [{"name": "priority-unknown"}]}) == {
        "tags": ["priority-unknown"]}
    assert adapter.map_fields({"milestone": {"due_on": None}}) == {}
    assert adapter.map_fields({}) == {}


# --------------------------------------------------------------------------- #
# GitHub: push
# --------------------------------------------------------------------------- #


def test_push_done_with_pr_sends_close_and_comment(server):
    task = TaskRecord(external_ref="github:example/repo#7", status="done", pr_url=PR_URL)
    assert _gh_adapter(server).push(task) == "github:example/repo#7"

    reqs = server.requests
    assert [(r["method"], r["path"]) for r in reqs] == [
        ("PATCH", "/repos/example/repo/issues/7"),
        ("POST", "/repos/example/repo/issues/7/comments"),
    ]
    assert reqs[0]["json"] == {"state": "closed", "state_reason": "completed"}
    assert reqs[1]["json"] == {"body": f"macp: PR merged — {PR_URL}"}
    assert all(r["authorization"] == f"Bearer {GH_TOKEN}" for r in reqs)


def test_push_done_without_pr_closes_only(server):
    _gh_adapter(server).push(TaskRecord(external_ref="github:example/repo#7", status="done"))
    assert [(r["method"], r["path"]) for r in server.requests] == [
        ("PATCH", "/repos/example/repo/issues/7")]


def test_push_pr_on_open_task_comments_only(server):
    _gh_adapter(server).push(
        TaskRecord(external_ref="github:example/repo#7", status="open", pr_url=PR_URL))
    assert [(r["method"], r["path"]) for r in server.requests] == [
        ("POST", "/repos/example/repo/issues/7/comments")]


def test_push_targets_repo_named_in_ref(server):
    _gh_adapter(server).push(TaskRecord(external_ref="github:other-org/other.repo#12", status="done"))
    assert server.requests[0]["path"] == "/repos/other-org/other.repo/issues/12"


@pytest.mark.parametrize("ref", ["jira:PROJ-1", "github:nohash", "github:example/repo#x"])
def test_push_bad_ref_raises_without_request(server, ref):
    with pytest.raises(TrackerError) as excinfo:
        _gh_adapter(server).push(TaskRecord(external_ref=ref, status="done", pr_url=PR_URL))
    assert ref in str(excinfo.value)
    assert server.requests == []


def test_push_bad_ref_with_nothing_to_send_is_noop(server):
    assert _gh_adapter(server).push(TaskRecord(external_ref="jira:PROJ-1", status="open")) == (
        "jira:PROJ-1")
    assert server.requests == []


# --------------------------------------------------------------------------- #
# GitHub: from_config opt-in gate
# --------------------------------------------------------------------------- #


def _clear_env(monkeypatch, names=GH_ENV + ("GITHUB_TOKEN",)) -> None:
    for var in names:
        monkeypatch.delenv(var, raising=False)


def test_from_config_reads_config_and_secrets(server, tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text(
        f'github_issues_repo = "example/repo"\ngithub_api_url = "{server.base_url}"\n',
        encoding="utf-8",
    )
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "github.json").write_text(
        json.dumps({"token": GH_TOKEN}), encoding="utf-8")

    adapter = GitHubIssuesAdapter.from_config(vault_root=tmp_path, start=tmp_path)
    assert isinstance(adapter, GitHubIssuesAdapter)
    assert adapter.repo == "example/repo"
    server.gh_issues = [_gh_issue()]
    assert len(adapter.pull()) == 1
    assert server.requests[0]["authorization"] == f"Bearer {GH_TOKEN}"


def test_from_config_env_overrides(server, tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACP_GITHUB_ISSUES_REPO", "example/repo")
    monkeypatch.setenv("MACP_GITHUB_API_URL", server.base_url)
    monkeypatch.setenv("MACP_GITHUB_TOKEN", GH_TOKEN)
    adapter = GitHubIssuesAdapter.from_config(vault_root=tmp_path, start=tmp_path)
    assert adapter is not None and adapter.base_url == server.base_url


def test_from_config_ignores_generic_github_token(server, tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACP_GITHUB_ISSUES_REPO", "example/repo")
    monkeypatch.setenv("GITHUB_TOKEN", GH_TOKEN)     # ambient token must not switch sync on
    assert GitHubIssuesAdapter.from_config(vault_root=tmp_path, start=tmp_path) is None
    assert server.requests == []


@pytest.mark.parametrize("repo", ["not-a-repo", "a/b/c", "owner/ repo"])
def test_from_config_rejects_malformed_repo(tmp_path, monkeypatch, repo):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MACP_GITHUB_ISSUES_REPO", repo)
    monkeypatch.setenv("MACP_GITHUB_TOKEN", GH_TOKEN)
    assert GitHubIssuesAdapter.from_config(vault_root=tmp_path, start=tmp_path) is None


def test_load_github_token_bad_secrets_file_reads_as_unset(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "github.json").write_text("[1, 2]", encoding="utf-8")
    assert base.load_github_token(tmp_path) is None
    (tmp_path / ".secrets" / "github.json").write_text("{not json", encoding="utf-8")
    assert base.load_github_token(tmp_path) is None


def test_jira_tracker_error_is_shared():
    assert jira.TrackerError is base.TrackerError


# --------------------------------------------------------------------------- #
# Tracker contract — parameterized over Jira and GitHub
# --------------------------------------------------------------------------- #


@dataclass
class Case:
    name: str
    server: RecordingTrackerServer
    adapter: TrackerAdapter
    token: str
    seed: Callable[[], None]
    ref_prefix: str
    done_ref: str
    env_vars: tuple[str, ...]
    from_config: Callable[..., object]

    def auth_carries_token(self, auth: str) -> bool:
        scheme, _, value = auth.partition(" ")
        if scheme == "Basic":
            return base64.b64decode(value).decode("utf-8").endswith(f":{self.token}")
        return scheme == "Bearer" and value == self.token


@pytest.fixture(params=["jira", "github"])
def contract(request, server) -> Case:
    if request.param == "jira":
        def seed() -> None:
            server.jira_issues = [_jira_issue()]

        return Case(
            name="jira", server=server, adapter=_jira_adapter(server), token=JIRA_TOKEN,
            seed=seed, ref_prefix="jira:", done_ref="jira:PROJ-123",
            env_vars=JIRA_ENV, from_config=JiraAdapter.from_config,
        )

    def seed() -> None:
        server.gh_issues = [_gh_issue(), _gh_pr_item()]

    return Case(
        name="github", server=server, adapter=_gh_adapter(server), token=GH_TOKEN,
        seed=seed, ref_prefix="github:", done_ref="github:example/repo#7",
        env_vars=GH_ENV, from_config=GitHubIssuesAdapter.from_config,
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
    for var in contract.env_vars + ("GITHUB_TOKEN",):
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
    import todo_system.trackers.github_issues as gh_mod

    gh_mod = importlib.reload(gh_mod)
    assert gh_mod.GitHubIssuesAdapter.from_config(vault_root=tmp_path, start=tmp_path) is None
    assert not list(Path(tmp_path).glob("**/*.md"))
