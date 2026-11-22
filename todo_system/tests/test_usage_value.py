"""Usage vs results (todo_system/usage_value.py): the aggregation behind the
weekly brief table and the dashboard card.

Three layers, one section each:

* the pure core — period bounds, project key, kind classification, aggregation,
  number formatting and the wire model. No files, and the clock is always
  injected (``NOW``).
* the scanner — real-shaped run dirs, task files and ledger rows unpacked from
  ``usage_value_cases/*.json`` into a tmp vault.
* the brief section and the CLI.

Every value here is synthetic. The fixtures keep only the SHAPE of real runs
(keys, file names, frontmatter fields); slugs, repos, PR numbers and token
counts are made up, because this file ships in the public tree.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from todo_system import usage_value as uv
from todo_system.orchestrator import discovery

NOW = dt.datetime(2026, 11, 22, 18, 0, 0)
APP = "https://github.com/example/app.git"
ENGINE = "https://github.com/example/macp.git"
_HANGUL = re.compile("[가-힣]")


def _run(name: str, out: int = 0, *, unmetered: bool = False, pr: str | None = None,
         merged: bool | None = None) -> dict:
    """One run record as scan() hands it to aggregate()."""
    at, slug = uv.parse_run_name(name)
    return {"name": name, "slug": slug, "at": at, "out_tokens": out,
            "unmetered": unmetered, "pr_url": pr, "merged": merged}


def _pr(n: int, repo: str = "example/app") -> str:
    return f"https://github.com/{repo}/pull/{n}"


def _period(raw: dict, pid: str) -> dict:
    return next(p for p in raw["periods"] if p["id"] == pid)


def _row(raw: dict, pid: str, project: str, kind: str) -> dict:
    rows = [r for r in _period(raw, pid)["rows"]
            if (r["project"], r["kind"]) == (project, kind)]
    assert len(rows) == 1, _period(raw, pid)["rows"]
    return rows[0]


# --- periods ----------------------------------------------------------------------


def test_period_bounds_week_and_month():
    b = uv.period_bounds(NOW)
    assert b["week"] == (dt.datetime(2026, 11, 16), NOW)
    assert b["month"] == (dt.datetime(2026, 11, 1), NOW)


def test_period_bounds_week_crosses_month_start():
    now = dt.datetime(2026, 12, 2, 9, 0, 0)
    b = uv.period_bounds(now)
    assert b["week"] == (dt.datetime(2026, 11, 26), now)
    assert b["month"] == (dt.datetime(2026, 12, 1), now)


def test_run_on_week_start_counts_and_one_second_before_does_not():
    tasks = {"app-a": {"repo": APP}, "app-b": {"repo": APP}}
    raw = uv.aggregate([_run("20261116T000000-app-a", 10), _run("20261115T235959-app-b", 7)],
                       tasks, NOW)
    assert _period(raw, "week")["total"]["runs"] == 1
    assert _period(raw, "week")["total"]["out_tokens"] == 10
    assert _period(raw, "month")["total"]["runs"] == 2       # both are in November


def test_run_on_month_start_counts_and_one_second_before_does_not():
    tasks = {"app-a": {"repo": APP}, "app-b": {"repo": APP}}
    raw = uv.aggregate([_run("20261101T000000-app-a", 10), _run("20261031T235959-app-b", 7)],
                       tasks, NOW)
    assert _period(raw, "month")["total"]["runs"] == 1
    assert _period(raw, "month")["total"]["out_tokens"] == 10
    assert _period(raw, "week")["total"]["runs"] == 0


def test_run_after_now_is_excluded():
    tasks = {"app-a": {"repo": APP}}
    raw = uv.aggregate([_run("20261122T180000-app-a", 10), _run("20261122T180001-app-a", 99)],
                       tasks, NOW)
    for pid in ("week", "month"):
        assert _period(raw, pid)["total"]["runs"] == 1, pid
        assert _period(raw, pid)["total"]["out_tokens"] == 10, pid


def test_parse_run_name_takes_the_slug_after_the_timestamp_only():
    assert uv.parse_run_name("20261121T144046-engine-api-poc") == (
        dt.datetime(2026, 11, 21, 14, 40, 46), "engine-api-poc")
    for bad in ("engine-api-poc", "2026-11-21-notes", "20261121T144046", "20261121T144046-",
                "20261340T999999-x", "x-20261121T144046-y"):
        assert uv.parse_run_name(bad) is None, bad


# --- kind classification: first match wins, the rule id is pinned --------------------


def test_classify_from_discovery_beats_everything():
    task = {"from_discovery": "2026-11-17-1210-discovery-app", "repo": ENGINE,
            "tags": ["todo", "macp", "security"], "worker_mode": "auto"}
    assert uv.classify(task, "security-fix-macp-something") == ("discovery", "from_discovery")
    # a written-out placeholder is not a discovery link
    assert uv.classify({"from_discovery": "-", "repo": APP}, "app-x")[0] != "discovery"


def test_classify_discovery_run_by_mode_and_slug():
    assert uv.classify({"worker_mode": "discover", "discover_repo": APP}, "anything") == (
        "discovery", "discover_run")
    # no task file left: the slug alone still says it
    assert uv.classify({}, "app-discovery-20261117") == ("discovery", "discover_run")
    assert uv.classify(None, "app-discovery-20261117") == ("discovery", "discover_run")
    # the date suffix is the whole marker, not a substring
    assert uv.classify({"repo": APP}, "app-discovery-notes")[0] != "discovery"


def test_classify_engine_repo_is_self_even_with_security_tag_and_prefix():
    # The boundary case: an engine security fix carries all three signals at once.
    task = {"repo": ENGINE, "tags": ["todo", "security", "worker"], "worker_mode": "auto"}
    assert uv.classify(task, "security-fix-macp-lock-deps") == ("self", "self_repo")
    # the same tags and prefix in another project are security work
    assert uv.classify({**task, "repo": APP}, "security-fix-app-lock-deps") == (
        "security", "tag:security")


def test_classify_self_by_tag_and_by_slug_prefix():
    assert uv.classify({"tags": ["todo", "macp", "feature"]}, "dashboard-card") == (
        "self", "self_tag")
    assert uv.classify({"tags": ["todo"]}, "macp-queue-order") == ("self", "self_slug")
    assert uv.classify({"repo": "https://github.com/example/macp-dashboard-android.git",
                        "tags": ["todo", "android"]}, "macp-dashboard-android-card") == (
        "self", "self_slug")
    # the prefix needs its dash: `macpro-…` is someone else's slug
    assert uv.classify({"tags": ["todo"]}, "macpro-setup") == ("other", "none")


def test_classify_tag_precedence():
    c = uv.classify
    assert c({"repo": APP, "tags": ["feature", "bug", "security"]}, "x") == ("security", "tag:security")
    assert c({"repo": APP, "tags": ["feature", "bugfix"]}, "x") == ("bug", "tag:bugfix")
    assert c({"repo": APP, "tags": ["feature", "regression"]}, "x") == ("bug", "tag:regression")
    assert c({"repo": APP, "tags": ["feature", "experiment"]}, "x") == ("research", "tag:experiment")
    assert c({"repo": APP, "tags": ["paper"]}, "x") == ("research", "tag:paper")
    assert c({"repo": APP, "tags": ["feature", "infra"]}, "x") == ("ops", "tag:infra")
    assert c({"repo": APP, "tags": ["maintenance"]}, "x") == ("ops", "tag:maintenance")
    assert c({"repo": APP, "tags": ["todo", "feature"]}, "x") == ("feature", "tag:feature")
    # tags are matched whole and case-insensitively, as a list or a comma string
    assert c({"repo": APP, "tags": "todo, Security"}, "x") == ("security", "tag:security")
    assert c({"repo": APP, "tags": ["debug", "bugs"]}, "x") == ("other", "none")
    # a tag beats the slug prefix
    assert c({"repo": APP, "tags": ["feature"]}, "fix-login") == ("feature", "tag:feature")


def test_classify_slug_prefix_when_no_tag():
    c = uv.classify
    task = {"repo": APP, "tags": ["todo", "work"]}
    assert c(task, "security-fix-app-deps") == ("security", "slug:security-fix-")
    assert c(task, "security-headers") == ("security", "slug:security-")
    assert c(task, "fix-login") == ("bug", "slug:fix-")
    assert c(task, "hotfix-crash") == ("bug", "slug:hotfix-")
    assert c(task, "research-pricing") == ("research", "slug:research-")
    assert c(task, "infra-backup") == ("ops", "slug:infra-")
    assert c(task, "feat-share-sheet") == ("feature", "slug:feat-")
    assert c(task, "feature-share-sheet") == ("feature", "slug:feature-")


def test_classify_other_when_no_signal():
    assert uv.classify({"repo": APP, "tags": ["todo", "work"]}, "app-settings") == ("other", "none")
    assert uv.classify({}, "weekly-notes") == ("other", "none")
    assert uv.classify(None, "weekly-notes") == ("other", "none")


def test_every_rule_names_a_known_kind():
    kinds = {k for _, k in uv._TAG_KINDS} | {k for _, k in uv._SLUG_PREFIX_KINDS}
    assert kinds <= set(uv.KINDS)
    assert uv.KINDS == ("discovery", "self", "feature", "bug", "security", "research",
                        "ops", "other")


# --- project key -----------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://github.com/example/macp.git",
    "https://github.com/example/macp",
    "https://github.com/example/macp/",
    "git@github.com:example/macp.git",
    "ssh://git@git.example.org/another-owner/MACP.git",
])
def test_repo_match_is_by_name_not_url(url):
    task = {"repo": url, "tags": ["todo"]}
    assert uv.project_of(task, "some-task") == "macp"
    assert uv.classify(task, "some-task") == ("self", "self_repo")


def test_an_owner_named_like_the_engine_is_not_the_engine():
    task = {"repo": "https://github.com/macp/other-app.git", "tags": ["todo"]}
    assert uv.project_of(task, "some-task") == "other-app"
    assert uv.classify(task, "some-task") == ("other", "none")


def test_project_of_repo_discover_repo_slug_and_vault():
    assert uv.project_of({"repo": "https://github.com/example/DripCoffee.git"}, "t") == "dripcoffee"
    assert uv.project_of({"repo": "-"}, "t") == "vault"
    assert uv.project_of({}, "t") == "vault"
    assert uv.project_of(None, "t") == "vault"
    # a discovery run reads discover_repo, not the task's (absent) repo
    assert uv.project_of({"worker_mode": "discover", "discover_repo": APP}, "app-discovery-20261117") == "app"
    # without the task file the slug's own prefix is the project
    assert uv.project_of({}, "if-then-app-discovery-20261117") == "if-then-app"
    # a task born from a discovery report is that repo's row
    assert uv.project_of({"from_discovery": "r", "repo": APP}, "app-fix-crash") == "app"


@pytest.mark.parametrize("url", [
    APP, ENGINE, "https://github.com/example/DripCoffee.git", "git@github.com:example/if_then.habits.git",
    "https://github.com/example/app/", "plain-name", "",
])
def test_project_name_matches_discovery_project_name(url):
    # usage_value keeps its own copy so importing it stays light (discovery pulls
    # repos and a vault root at import); this pins the two together.
    assert uv.project_name(url) == discovery.project_name(url)


# --- aggregation ---------------------------------------------------------------------------


def test_aggregate_sums_tokens_runs_and_unmetered():
    tasks = {"app-a": {"repo": APP, "tags": ["todo", "feature"], "status": "open"}}
    raw = uv.aggregate([
        _run("20261120T100000-app-a", 100_000),
        _run("20261121T100000-app-a", 50_000),
        _run("20261122T100000-app-a", 0, unmetered=True),
    ], tasks, NOW)
    row = _row(raw, "week", "app", "feature")
    assert (row["out_tokens"], row["runs"], row["runs_unmetered"]) == (150_000, 3, 1)
    total = _period(raw, "week")["total"]
    assert (total["out_tokens"], total["runs"], total["runs_unmetered"]) == (150_000, 3, 1)


def test_merged_prs_are_distinct_across_retries():
    tasks = {"app-a": {"repo": APP, "tags": ["feature"], "status": "done"},
             "app-b": {"repo": APP, "tags": ["feature"], "status": "done"}}
    raw = uv.aggregate([
        # three attempts of one task, all on the same PR
        _run("20261118T100000-app-a", 10, pr=_pr(1), merged=False),
        _run("20261119T100000-app-a", 10, pr=_pr(1), merged=False),
        _run("20261120T100000-app-a", 10, pr=_pr(1) + "/", merged=True),
        _run("20261121T100000-app-b", 10, pr=_pr(2), merged=True),
    ], tasks, NOW)
    assert _row(raw, "week", "app", "feature")["merged_prs"] == 2
    assert _period(raw, "week")["total"]["merged_prs"] == 2


def test_done_task_counts_only_its_latest_pr():
    # A closed retry PR (#1) must not count: the done task's PR is its last one.
    tasks = {"app-a": {"repo": APP, "tags": ["feature"], "status": "done"}}
    raw = uv.aggregate([
        _run("20261118T100000-app-a", 10, pr=_pr(1), merged=False),
        _run("20261119T100000-app-a", 10, pr=_pr(2), merged=False),   # merged later by hand
        _run("20261120T100000-app-a", 10),                              # a no-PR rerun
    ], tasks, NOW)
    assert _row(raw, "week", "app", "feature")["merged_prs"] == 1
    # an explicit `merged: yes` on the earlier PR is counted next to the last one
    raw = uv.aggregate([
        _run("20261118T100000-app-a", 10, pr=_pr(1), merged=True),
        _run("20261119T100000-app-a", 10, pr=_pr(2), merged=False),
    ], tasks, NOW)
    assert _row(raw, "week", "app", "feature")["merged_prs"] == 2


def test_open_task_with_unmerged_pr_counts_zero():
    tasks = {"app-a": {"repo": APP, "tags": ["feature"], "status": "open", "state": "awaiting_human"}}
    raw = uv.aggregate([_run("20261120T100000-app-a", 10, pr=_pr(1), merged=False),
                        _run("20261121T100000-app-a", 10, pr=_pr(1))], tasks, NOW)
    row = _row(raw, "week", "app", "feature")
    assert (row["merged_prs"], row["tokens_per_merge"]) == (0, None)
    # `merged: yes` with no PR named is not evidence of a PR
    raw = uv.aggregate([_run("20261120T100000-app-a", 10, merged=True)], tasks, NOW)
    assert _row(raw, "week", "app", "feature")["merged_prs"] == 0


def test_done_cancelled_failed_are_distinct_tasks_by_current_state():
    base = {"repo": APP, "tags": ["feature"]}
    tasks = {
        "t-done": {**base, "status": "done", "state": "done"},
        "t-done-ledger-failed": {**base, "status": "Done", "state": "failed"},   # md wins
        "t-cancelled": {**base, "status": "cancelled"},
        "t-canceled": {**base, "status": "canceled", "state": "failed"},
        "t-failed": {**base, "status": "open", "state": "failed", "deferred": False},
        "t-deferred": {**base, "status": "open", "state": "failed", "deferred": True},
        "t-parked": {**base, "status": "open", "state": "awaiting_human"},
        "t-running": {**base, "status": "open", "state": "in_progress"},
    }
    runs = [_run(f"20261120T10000{i}-{slug}", 1) for i, slug in enumerate(tasks)]
    runs.append(_run("20261121T100000-t-done", 1))          # a second run, the same task
    runs.append(_run("20261121T100001-t-failed", 1))
    raw = uv.aggregate(runs, tasks, NOW)
    row = _row(raw, "week", "app", "feature")
    assert (row["done"], row["cancelled"], row["failed"]) == (2, 2, 1)
    assert row["runs"] == 10
    total = _period(raw, "week")["total"]
    assert (total["done"], total["cancelled"], total["failed"]) == (2, 2, 1)


def test_tokens_per_merge_is_null_without_merges():
    tasks = {"app-a": {"repo": APP, "tags": ["feature"], "status": "done"},
             "eng-a": {"repo": ENGINE, "status": "open"}}
    raw = uv.aggregate([
        _run("20261120T100000-app-a", 100_001, pr=_pr(1), merged=True),
        _run("20261121T100000-app-a", 100_000, pr=_pr(2), merged=True),
        _run("20261121T110000-app-a", 100_000, pr=_pr(3), merged=True),
        _run("20261121T120000-eng-a", 5),
    ], tasks, NOW)
    assert _row(raw, "week", "app", "feature")["tokens_per_merge"] == 100_000   # floor(300001 / 3)
    assert _row(raw, "week", "macp", "self")["tokens_per_merge"] is None
    assert _period(raw, "week")["total"]["tokens_per_merge"] == 300_006 // 3


def test_rows_sorted_and_total_matches():
    tasks = {
        "eng-a": {"repo": ENGINE, "status": "done"},
        "app-a": {"repo": APP, "tags": ["feature"], "status": "done"},
        "app-b": {"repo": APP, "tags": ["bug"], "status": "open", "state": "failed"},
        "zed-a": {"repo": "https://github.com/example/zed.git", "tags": ["bug"], "status": "cancelled"},
        "note-a": {"status": "open"},
    }
    raw = uv.aggregate([
        _run("20261120T100000-eng-a", 900, pr=_pr(1, "example/macp"), merged=True),
        _run("20261120T110000-app-a", 500, pr=_pr(1), merged=True),
        _run("20261120T120000-app-b", 300),
        _run("20261120T130000-app-b", 0, unmetered=True),      # 300 tokens, 2 runs
        _run("20261120T140000-zed-a", 300),                    # 300 tokens, 1 run
        _run("20261120T150000-note-a", 300),                   # 300 tokens, 1 run, vault
        _run("20261005T150000-eng-a", 4000),                   # October: in neither period
    ], tasks, NOW)
    week = _period(raw, "week")
    assert [(r["project"], r["kind"]) for r in week["rows"]] == [
        ("macp", "self"), ("app", "feature"), ("app", "bug"), ("vault", "other"), ("zed", "bug")]
    for key in ("out_tokens", "runs", "runs_unmetered", "merged_prs", "done", "cancelled", "failed"):
        assert week["total"][key] == sum(r[key] for r in week["rows"]), key
    assert week["total"] == {"out_tokens": 2300, "runs": 6, "runs_unmetered": 1, "merged_prs": 2,
                             "done": 2, "cancelled": 1, "failed": 1, "tokens_per_merge": 1150}
    assert _period(raw, "month")["rows"] == week["rows"]


def test_a_pr_shared_by_two_rows_is_one_merge_in_the_total():
    tasks = {"app-a": {"repo": APP, "tags": ["feature"], "status": "done"},
             "app-b": {"repo": APP, "tags": ["bug"], "status": "done"}}
    raw = uv.aggregate([_run("20261120T100000-app-a", 10, pr=_pr(7), merged=True),
                        _run("20261120T110000-app-b", 10, pr=_pr(7), merged=True)], tasks, NOW)
    week = _period(raw, "week")
    assert [r["merged_prs"] for r in week["rows"]] == [1, 1]
    assert week["total"]["merged_prs"] == 1


def test_empty_input_gives_two_empty_periods():
    raw = uv.aggregate([], {}, NOW)
    assert [p["id"] for p in raw["periods"]] == ["week", "month"]
    for p in raw["periods"]:
        assert p["rows"] == []
        assert p["total"] == {"out_tokens": 0, "runs": 0, "runs_unmetered": 0, "merged_prs": 0,
                              "done": 0, "cancelled": 0, "failed": 0, "tokens_per_merge": None}


# --- presentation: numbers and the wire model ---------------------------------------------------


def test_fmt_tokens_values():
    cases = {0: "0", 950: "950", 999: "999", 1_000: "1.0k", 1_500: "1.5k", 99_949: "99.9k",
             99_950: "100k", 205_400: "205k", 999_499: "999k", 999_500: "1.0M",
             4_123_456: "4.1M", 15_600_000: "15.6M", 123_400_000: "123M",
             2_500_000_000: "2500M", None: "—"}
    for n, want in cases.items():
        assert uv.fmt_tokens(n) == want, n


def _sample_raw() -> dict:
    tasks = {"engine-triage-cards": {"repo": ENGINE, "tags": ["todo", "macp", "feature"],
                                     "status": "done", "state": "done"},
             "app-a": {"repo": APP, "tags": ["feature"], "status": "open", "state": "failed"}}
    return uv.aggregate([
        _run("20261121T144046-engine-triage-cards", 466_000, pr=_pr(12, "example/macp"), merged=True),
        _run("20261103T090000-app-a", 1_200, unmetered=True),
    ], tasks, NOW)


def test_wire_model_keys_are_the_contract():
    model = uv.present(_sample_raw())
    assert list(model) == ["title", "basis", "periods"]
    cell = ["out_tokens", "out_tokens_text", "runs", "runs_unmetered", "merged_prs", "done",
            "cancelled", "failed", "tokens_per_merge", "tokens_per_merge_text", "text"]
    assert [p["id"] for p in model["periods"]] == ["week", "month"]
    for p in model["periods"]:
        assert list(p) == ["id", "label", "range", "start", "end", "total", "rows"]
        assert list(p["total"]) == cell
        for row in p["rows"]:
            assert list(row) == ["project", "kind", "kind_label", *cell]
            assert row["kind"] in uv.KINDS
    week, month = model["periods"]
    assert (week["start"], week["end"], week["range"]) == ("2026-11-16", "2026-11-22", "11/16–11/22")
    assert (month["start"], month["end"], month["range"]) == ("2026-11-01", "2026-11-22", "11/1–11/22")


def test_model_is_json_and_follows_language(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    model = uv.present(_sample_raw())
    assert json.loads(json.dumps(model)) == model
    assert model["title"] == "사용량 대비 결과"
    assert "하한" in model["basis"]
    week, month = model["periods"]
    assert (week["label"], month["label"]) == ("지난 7일", "이번 달")
    row = week["rows"][0]
    assert (row["project"], row["kind"], row["kind_label"]) == ("macp", "self", "macp 자기개선")
    assert (row["out_tokens_text"], row["tokens_per_merge_text"]) == ("466k", "466k")
    assert row["text"] == "런 1 · 머지 PR 1 · 완료 1 · 취소 0 · 실패 0 · 머지당 466k"
    other = month["rows"][1]
    assert (other["project"], other["kind_label"]) == ("app", "기능")
    assert other["tokens_per_merge_text"] == "—"
    assert other["text"] == "런 1 · 머지 PR 0 · 완료 0 · 취소 0 · 실패 1 · 머지당 — · 미계측 런 1"
    assert month["total"]["text"].endswith(" · 미계측 런 1")
    assert month["total"]["out_tokens_text"] == "467k"

    monkeypatch.setenv("MACP_LANGUAGE", "en")
    en = uv.present(_sample_raw())
    assert not _HANGUL.search(json.dumps(en, ensure_ascii=False))
    assert en["periods"][0]["rows"][0]["kind_label"] == "macp self-improvement"
    # the numbers and ids do not move with the language
    for a, b in zip(model["periods"], en["periods"]):
        assert (a["id"], a["range"], a["start"], a["end"]) == (b["id"], b["range"], b["start"], b["end"])
        assert [(r["project"], r["kind"], r["out_tokens"]) for r in a["rows"]] == [
            (r["project"], r["kind"], r["out_tokens"]) for r in b["rows"]]
