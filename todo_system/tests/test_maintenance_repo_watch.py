"""The public-repo issue/PR watch (todo_system/maintenance_repo_watch.py).

An external contributor's issue once sat open for 17 days with nobody noticing:
nothing in the engine looked at the public repo's issue/PR list. This step does,
once a day, and turns every external item still waiting on the maintainer into an
``ops-repo-…`` human task (``ops_alerts.open_alert``) that the dashboard lists
under "직접 할 일".

Every seam is injected — no test here reaches a real ``gh`` or ``git``:

* ``_run`` is a :class:`FakeRun` that answers ``gh api user``, ``gh api graphql``
  and ``git remote get-url origin`` from canned data and records every call (the
  budget tests count them);
* ``STATE_DIR`` and ``ops_alerts.VAULT_ROOT`` point into ``tmp_path``;
* the clock is passed as ``now=`` and ``ops_alerts._today`` is pinned.

Fictional names only (the public identifier gate): ``example-owner/example-repo``,
``alice``, ``bob``, ``example-maint``.
"""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from todo_system import maintenance_repo_watch as rw
from todo_system import ops_alerts

REPO = "example-owner/example-repo"
OTHER = "example-owner/other-repo"
MAINT = "example-maint"
T0 = 1_800_000_000.0
DAY = 24 * 3600


# -- canned GitHub data ------------------------------------------------------

def issue(n, *, author="alice", title=None, body="이슈 본문", comments=()):
    return {
        "number": n,
        "title": title if title is not None else f"외부 이슈 {n}",
        "url": f"https://github.com/{REPO}/issues/{n}",
        "createdAt": "2026-10-27T09:00:00Z",
        "body": body,
        "author": {"login": author} if author is not None else None,
        "comments": {"nodes": [{"author": {"login": a}} for a in comments]},
    }


def pr(n, *, author="bob", head="feature/x", cross=True, comments=(), reviews=(),
       repo=REPO):
    return {
        "number": n,
        "title": f"외부 PR {n}",
        "url": f"https://github.com/{repo}/pull/{n}",
        "createdAt": "2026-11-01T09:00:00Z",
        "body": "PR 본문",
        "headRefName": head,
        "isCrossRepository": cross,
        "author": {"login": author},
        "comments": {"nodes": [{"author": {"login": a}} for a in comments]},
        "reviews": {"nodes": [{"author": {"login": a}} for a in reviews]},
    }


def repo_payload(issues=(), prs=(), *, issue_total=None, pr_total=None):
    issues, prs = list(issues), list(prs)
    return {"data": {"repository": {
        "issues": {"totalCount": len(issues) if issue_total is None else issue_total,
                   "nodes": issues},
        "pullRequests": {"totalCount": len(prs) if pr_total is None else pr_total,
                         "nodes": prs},
    }}}


def _done(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


class FakeRun:
    """Answers the three commands the step may run; records every argv."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.user: object = _done(json.dumps({"login": MAINT}))
        self.graphql: dict[str, object] = {REPO: repo_payload()}
        self.origin: object = _done(f"https://github.com/{REPO}.git\n")

    def __call__(self, argv, **_kw):
        argv = list(argv)
        self.calls.append(argv)
        if argv[:3] == ["gh", "api", "user"]:
            answer = self.user
        elif argv[:3] == ["gh", "api", "graphql"]:
            fields = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("query="))
            answer = self.graphql.get(f"{fields.get('owner')}/{fields.get('name')}",
                                      _done(rc=1, stderr="Could not resolve to a Repository"))
        elif argv[:1] == ["git"] and "get-url" in argv:
            answer = self.origin
        else:                                       # pragma: no cover - a new command
            raise AssertionError(f"unexpected command {argv}")
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, dict):
            return _done(json.dumps(answer))
        return answer

    def gh_calls(self):
        return [c for c in self.calls if c[:1] == ["gh"]]


@pytest.fixture
def watch(monkeypatch, tmp_path):
    """Lift the suite-wide kill switch and point every seam at tmp. Requested (not
    autouse), so it runs AFTER conftest's ``_repo_watch_isolated`` and wins."""
    fake = FakeRun()
    monkeypatch.setenv(rw.KILL_ENV, "1")
    monkeypatch.setenv(rw.REPOS_ENV, REPO)
    monkeypatch.setattr(rw, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(rw, "CONFIG_START", tmp_path / "no-config")
    monkeypatch.setattr(rw, "_run", fake)
    monkeypatch.setattr(rw, "_dist_repo_url", lambda: None)
    monkeypatch.setattr(ops_alerts, "VAULT_ROOT", tmp_path / "vault")
    monkeypatch.setattr(ops_alerts, "_today", lambda: "2026-11-15")
    return fake


def task_path(key):
    return ops_alerts._path(key)


def task_text(key):
    return task_path(key).read_text(encoding="utf-8")


def status(key):
    return ops_alerts._read_field(task_text(key), "status")


def frontmatter(key):
    text = task_text(key)
    assert text.startswith("---\n")
    head = text.split("\n---\n", 1)[0][4:]
    return yaml.safe_load(head)


def preopen(key, repo=REPO, kind="issues", n=5):
    ops_alerts.open_alert(key, f"pre-existing {key}",
                          [f"확인·답글·머지 여부 결정 — https://github.com/{repo}/{kind}/{n}"])
    assert status(key) == "open"


# -- opening -----------------------------------------------------------------

def test_new_external_issue_opens_a_manual_task_with_the_details(watch):
    sentinel = "SENTINEL-AFTER-200"
    body = ("가" * 210) + sentinel + ("나" * 280)
    watch.graphql[REPO] = repo_payload([issue(7, title="로그인 오류", body=body)])

    line = rw.watch_tick(now=T0)

    assert line is not None and line.startswith("repo-watch:") and "\n" not in line
    assert "1건 열림" in line
    fm = frontmatter("repo-issue-7")
    assert fm["status"] == "open"
    assert fm["worker_mode"] == "manual"
    text = task_text("repo-issue-7")
    assert "로그인 오류" in text
    assert "@alice" in text
    assert f"https://github.com/{REPO}/issues/7" in text
    assert "2026-10-27" in text
    assert "확인·답글·머지 여부 결정" in text
    assert sentinel not in text, "only the first 200 chars of the body may be copied"
    excerpt_line = next(ln for ln in text.splitlines() if "본문 첫 200자" in ln)
    excerpt = excerpt_line.split("본문 첫 200자:", 1)[1].strip()
    assert 0 < len(excerpt) <= rw.EXCERPT_CHARS


def test_external_fork_pr_opens_a_task(watch):
    watch.graphql[REPO] = repo_payload(prs=[pr(8)])

    rw.watch_tick(now=T0)

    assert status("repo-pr-8") == "open"
    assert "@bob" in task_text("repo-pr-8")
    assert f"https://github.com/{REPO}/pull/8" in task_text("repo-pr-8")


def test_a_deleted_author_counts_as_external(watch):
    watch.graphql[REPO] = repo_payload([issue(9, author=None)])

    rw.watch_tick(now=T0)

    assert status("repo-issue-9") == "open"
    assert "@ghost" in task_text("repo-issue-9")


@pytest.mark.parametrize("node", [
    issue(11, author="Example-Maint"),                      # maintainer's own, case-varied
    pr(12, head="worker/foo", cross=True),                  # a worker PR, even from a fork
    pr(13, head="feature/y", cross=False),                  # a same-repo (maintainer) branch
    pr(14, author="EXAMPLE-MAINT"),                         # maintainer's own fork PR
], ids=["own-issue", "worker-pr", "same-repo-pr", "own-pr"])
def test_exclusions_open_nothing(watch, node):
    kind_issues = "headRefName" not in node
    watch.graphql[REPO] = (repo_payload([node]) if kind_issues
                           else repo_payload(prs=[node]))

    assert rw.watch_tick(now=T0) is None
    items = ops_alerts.vault_root() / "Todo" / "items"
    assert not items.exists() or list(items.glob("ops-repo-*.md")) == []


# -- resolving ---------------------------------------------------------------

def test_a_maintainer_comment_resolves_the_issue_task(watch):
    preopen("repo-issue-5", n=5)
    watch.graphql[REPO] = repo_payload([issue(5, comments=["alice", "Example-Maint"])])

    line = rw.watch_tick(now=T0)

    assert status("repo-issue-5") == "done"
    assert "1건 해소" in line


def test_a_maintainer_review_resolves_the_pr_task(watch):
    preopen("repo-pr-6", kind="pull", n=6)
    watch.graphql[REPO] = repo_payload(prs=[pr(6, reviews=[MAINT])])

    rw.watch_tick(now=T0)

    assert status("repo-pr-6") == "done"


def test_a_new_item_already_answered_opens_nothing(watch):
    watch.graphql[REPO] = repo_payload([issue(15, comments=[MAINT])],
                                       [pr(16, comments=[MAINT])])

    assert rw.watch_tick(now=T0) is None
    assert not task_path("repo-issue-15").exists()
    assert not task_path("repo-pr-16").exists()


def test_a_later_external_comment_does_not_reopen(watch):
    preopen("repo-issue-5", n=5)
    watch.graphql[REPO] = repo_payload([issue(5, comments=[MAINT, "alice"])])

    rw.watch_tick(now=T0)

    assert status("repo-issue-5") == "done"


def test_a_task_the_human_closed_is_not_revived(watch):
    """A person marking the task done without answering is a decision; the watch
    must not nag them back into it every day."""
    preopen("repo-issue-5", n=5)
    ops_alerts.resolve_alert("repo-issue-5", "사람이 닫음")
    watch.graphql[REPO] = repo_payload([issue(5)])

    rw.watch_tick(now=T0)

    assert status("repo-issue-5") == "done"


def test_a_closed_or_merged_item_resolves_by_absence(watch):
    preopen("repo-issue-5", n=5)
    preopen("repo-pr-6", kind="pull", n=6)
    watch.graphql[REPO] = repo_payload([issue(7)])

    line = rw.watch_tick(now=T0)

    assert status("repo-issue-5") == "done"
    assert status("repo-pr-6") == "done"
    assert status("repo-issue-7") == "open"
    assert "2건 해소" in line


def test_a_truncated_list_never_resolves_by_absence(watch):
    preopen("repo-issue-5", n=5)
    preopen("repo-pr-6", kind="pull", n=6)
    watch.graphql[REPO] = repo_payload([issue(7)], issue_total=150,
                                       pr_total=0)

    rw.watch_tick(now=T0)

    assert status("repo-issue-5") == "open", "issue #5 may be beyond the first page"
    assert status("repo-pr-6") == "done", "the PR list was complete"


def test_an_unrelated_ops_task_is_left_alone(watch):
    ops_alerts.open_alert("disk-low", "디스크 부족", ["정리한다"])
    watch.graphql[REPO] = repo_payload()

    rw.watch_tick(now=T0)

    assert status("disk-low") == "open"


# -- fail-safe ---------------------------------------------------------------

FAILURES = {
    "user-auth": ("user", _done(rc=1, stderr="gh: To get started with GitHub CLI, please run: gh auth login\nmore")),
    "graphql-rc": ("graphql", _done(rc=1, stderr="HTTP 502: Bad Gateway")),
    "timeout": ("graphql", subprocess.TimeoutExpired(cmd="gh", timeout=60)),
    "bad-json": ("graphql", _done("<html>not json")),
    "errors": ("graphql", {"errors": [{"message": "rate limited"}], "data": None}),
    "repo-null": ("graphql", {"data": {"repository": None}}),
    "gh-missing": ("user", FileNotFoundError("gh")),
}


@pytest.mark.parametrize("which", sorted(FAILURES))
def test_gh_failure_warns_once_and_closes_nothing(watch, monkeypatch, which):
    preopen("repo-issue-5", n=5)
    target, answer = FAILURES[which]
    if target == "user":
        watch.user = answer
    else:
        watch.graphql[REPO] = answer
    resolves: list = []
    real_resolve = ops_alerts.resolve_alert
    monkeypatch.setattr(ops_alerts, "resolve_alert",
                        lambda *a, **k: resolves.append(a) or real_resolve(*a, **k))

    line = rw.watch_tick(now=T0)

    assert line is not None
    assert line.startswith("repo-watch:") and "\n" not in line
    assert "gh 실패" in line
    assert resolves == []
    assert status("repo-issue-5") == "open"
    assert rw._read_state()["last_run_ts"] == 0.0

    before = len(watch.calls)
    assert rw.watch_tick(now=T0 + 30 * 60) is None
    assert len(watch.calls) == before, "a failed run must not retry every tick"

    watch.user = _done(json.dumps({"login": MAINT}))
    watch.graphql[REPO] = repo_payload([issue(7)])
    rw.watch_tick(now=T0 + 61 * 60)
    assert len(watch.gh_calls()) > 0 and len(watch.calls) > before, "retries after 1h"
    assert status("repo-issue-5") == "done"


def test_one_failing_repo_does_not_resolve_its_tasks_but_the_other_proceeds(watch, monkeypatch):
    monkeypatch.setenv(rw.REPOS_ENV, f"{REPO},{OTHER}")
    preopen("repo-issue-5", n=5)
    preopen("repo-issue-3-other-repo", repo=OTHER, n=3)
    watch.graphql[REPO] = _done(rc=1, stderr="HTTP 502")
    watch.graphql[OTHER] = repo_payload()

    line = rw.watch_tick(now=T0)

    assert status("repo-issue-5") == "open"
    assert status("repo-issue-3-other-repo") == "done"
    assert "gh 실패" in line and REPO in line
    assert rw._read_state()["last_run_ts"] == 0.0


def test_a_raising_runner_is_one_line_not_an_exception(watch, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("runner exploded")

    monkeypatch.setattr(rw, "_run", _boom)

    line = rw.watch_tick(now=T0)

    assert line is not None and line.startswith("repo-watch:") and "\n" not in line


# -- cadence and budget ------------------------------------------------------

def test_runs_once_a_day(watch):
    watch.graphql[REPO] = repo_payload([issue(7)])
    rw.watch_tick(now=T0)
    first = len(watch.calls)
    assert first > 0

    assert rw.watch_tick(now=T0 + 3600) is None
    assert len(watch.calls) == first

    rw.watch_tick(now=T0 + DAY + 1)
    assert len(watch.calls) > first

    state = json.loads(rw.state_path().read_text(encoding="utf-8"))
    assert state["last_run_ts"] == T0 + DAY + 1


def test_gh_budget_is_at_most_two_calls_per_repo(watch, monkeypatch):
    monkeypatch.setenv(rw.REPOS_ENV, f"{REPO},{OTHER}")
    watch.graphql[REPO] = repo_payload([issue(1), issue(2)], [pr(3)])
    watch.graphql[OTHER] = repo_payload([issue(4)])

    rw.watch_tick(now=T0)

    gh = watch.gh_calls()
    assert sum(1 for c in gh if c[:3] == ["gh", "api", "user"]) == 1
    assert sum(1 for c in gh if c[:3] == ["gh", "api", "graphql"]) == 2
    assert len(gh) <= 2 * 2


def test_steady_state_is_silent(watch):
    watch.graphql[REPO] = repo_payload([issue(7)])
    assert rw.watch_tick(now=T0) is not None
    text_before = task_text("repo-issue-7")

    assert rw.watch_tick(now=T0 + DAY + 1) is None
    assert task_text("repo-issue-7") == text_before, "no daily churn on an open task"


def test_kill_switch_makes_no_call(watch, monkeypatch):
    monkeypatch.setenv(rw.KILL_ENV, "0")

    assert rw.watch_tick(now=T0) is None
    assert watch.calls == []


@pytest.mark.parametrize("value,expected", [("0", False), ("", True), ("1", True)])
def test_only_an_explicit_zero_disables(monkeypatch, value, expected):
    monkeypatch.setenv(rw.KILL_ENV, value)
    assert rw.enabled() is expected


# -- sanitizing --------------------------------------------------------------

def test_hostile_title_and_body_keep_the_task_file_parseable(watch):
    title = 'Bad "quoted"\ntitle\r\twith \\ backslash: and # hash\n---\nstatus: done'
    body = 'line1\n---\n"quote" \\ end sep'
    watch.graphql[REPO] = repo_payload([issue(21, title=title, body=body)])

    rw.watch_tick(now=T0)

    fm = frontmatter("repo-issue-21")
    assert fm["status"] == "open"
    for field in ("outcome", "next_action"):
        assert "\n" not in fm[field] and '"' not in fm[field]
    text = task_text("repo-issue-21")
    headings = [ln for ln in text.splitlines() if ln.startswith("# ")]
    assert len(headings) == 1
    assert "Bad 'quoted' title with / backslash" in headings[0]
    assert ops_alerts._read_field(text, "status") == "open"


def test_clean_truncates_with_an_ellipsis_within_the_limit():
    out = rw._clean("x" * 500, 200)
    assert len(out) == 200 and out.endswith("…")
    assert rw._clean("  a \n\n b  ", 200) == "a b"
    assert rw._clean(None, 10) == ""


# -- config ladder -----------------------------------------------------------

def test_env_list_is_parsed_validated_and_deduped(watch, monkeypatch):
    monkeypatch.setenv(rw.REPOS_ENV,
                       " example-owner/a , example-owner/b ,bad slug, Example-Owner/A ,")
    repos, reason = rw.watched_repos()
    assert repos == ["example-owner/a", "example-owner/b"]
    assert reason is None


def test_config_key_is_read_when_env_is_unset(watch, monkeypatch, tmp_path):
    monkeypatch.delenv(rw.REPOS_ENV, raising=False)
    root = tmp_path / "cfgvault"
    (root / ".macp").mkdir(parents=True)
    (root / ".macp" / "config.toml").write_text(
        f'{rw.CONFIG_KEY} = "{OTHER}, {REPO}"\n', encoding="utf-8")
    monkeypatch.setattr(rw, "CONFIG_START", root / "sub")

    repos, _ = rw.watched_repos()

    assert repos == [OTHER, REPO]
    assert watch.calls == [], "a configured list needs no git call"


@pytest.mark.parametrize("url", [
    "https://github.com/o/r.git", "https://github.com/o/r", "https://github.com/o/r/",
    "git@github.com:o/r.git", "ssh://git@github.com/o/r.git", "https://user@github.com/o/r.git",
])
def test_origin_url_forms_parse(url):
    assert rw._parse_repo_url(url) == "o/r"


@pytest.mark.parametrize("url", ["https://gitlab.com/o/r.git", "", "/srv/git/r.git", None])
def test_non_github_urls_do_not_parse(url):
    assert rw._parse_repo_url(url) is None


def test_default_is_the_engine_origin(watch, monkeypatch):
    monkeypatch.delenv(rw.REPOS_ENV, raising=False)
    watch.origin = _done("git@github.com:example-owner/example-repo.git\n")

    repos, reason = rw.watched_repos()

    assert repos == [REPO] and reason is None


def test_metadata_is_the_last_fallback(watch, monkeypatch):
    monkeypatch.delenv(rw.REPOS_ENV, raising=False)
    watch.origin = _done(rc=128, stderr="fatal: not a git repository")
    monkeypatch.setattr(rw, "_dist_repo_url", lambda: f"https://github.com/{OTHER}")

    repos, reason = rw.watched_repos()

    assert repos == [OTHER] and reason is None


def test_nothing_to_watch_skips_once_a_day_with_no_gh_call(watch, monkeypatch):
    monkeypatch.delenv(rw.REPOS_ENV, raising=False)
    watch.origin = _done("https://gitlab.example/o/r.git\n")

    first = rw.watch_tick(now=T0)
    second = rw.watch_tick(now=T0 + 3600)

    assert first is not None and first.startswith("repo-watch:") and "\n" not in first
    assert second is None
    assert watch.gh_calls() == []


# -- multiple repos ----------------------------------------------------------

def test_a_second_repo_gets_suffixed_keys_and_never_touches_the_first(watch, monkeypatch):
    monkeypatch.setenv(rw.REPOS_ENV, f"{REPO},{OTHER}")
    preopen("repo-issue-5", n=5)
    other_issue = issue(5)
    other_issue["url"] = f"https://github.com/{OTHER}/issues/5"
    watch.graphql[REPO] = repo_payload([issue(5)])
    watch.graphql[OTHER] = repo_payload([other_issue], [pr(2, repo=OTHER)])

    rw.watch_tick(now=T0)

    assert status("repo-issue-5") == "open"
    assert status("repo-issue-5-other-repo") == "open"
    assert status("repo-pr-2-other-repo") == "open"
    assert OTHER in task_text("repo-issue-5-other-repo")


def test_absence_in_one_repo_does_not_resolve_anothers_task(watch, monkeypatch):
    monkeypatch.setenv(rw.REPOS_ENV, f"{OTHER},{REPO}")
    preopen("repo-issue-5-example-repo", repo=REPO, n=5)
    watch.graphql[OTHER] = repo_payload()
    watch.graphql[REPO] = repo_payload([issue(5)])

    rw.watch_tick(now=T0)

    assert status("repo-issue-5-example-repo") == "open"


# -- the CLI -----------------------------------------------------------------

def test_dry_run_makes_no_gh_call_and_writes_nothing(watch, capsys):
    assert rw.main(["--dry-run"]) == 0

    out = capsys.readouterr().out
    assert REPO in out
    assert watch.gh_calls() == []
    assert not rw.state_path().exists()


def test_the_cli_refuses_anything_but_dry_run(capsys):
    assert rw.main([]) == 2
    assert "--dry-run" in capsys.readouterr().err


def test_the_module_imports_only_stdlib_and_engine_siblings():
    import ast
    import sys
    from pathlib import Path

    source = Path(rw.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            imported.add(node.module.split(".")[0])
    assert imported - set(sys.stdlib_module_names) == set()
