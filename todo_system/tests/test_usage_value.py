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
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import usage_value as uv
from todo_system.orchestrator import discovery

NOW = dt.datetime(2026, 11, 22, 18, 0, 0)
APP = "https://github.com/example/app.git"
ENGINE = "https://github.com/example/macp.git"
_HANGUL = re.compile("[가-힣]")
_REPO = Path(__file__).resolve().parents[2]
_CASES = Path(__file__).resolve().parent / "usage_value_cases"
CASE_NAMES = ("auto_five_stage_merged", "quick_merged", "delegate_vault_done",
              "discovery_run_and_child", "unmetered_runs")
_LOADERS = ("_load_usage", "_load_report", "_load_quick", "_load_task", "_load_ledger")


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


# --- the scanner: real-shaped files in a tmp vault ---------------------------------------------


def _case(name: str) -> dict:
    return json.loads((_CASES / f"{name}.json").read_text(encoding="utf-8"))


def _content(value) -> str:
    """A fixture file: an object is JSON, a list is its lines, a string is itself."""
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, list):
        return "\n".join(value) + "\n"
    return value


def _usage(out: int) -> dict:
    return {"input_tokens": 1, "output_tokens": out, "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3, "num_turns": 4, "duration_ms": 5, "model": "model-a",
            "session_id": "00000000-0000-4000-8000-0000000000aa", "total_cost_usd": 0.1}


def _report(pr: str = "null", merged: str = "n/a") -> list[str]:
    return ["---", "stage: report", "overall_status: pass", f"pr_url: {pr}", f"merged: {merged}",
            "no_op: false", "---", "", "# Report"]


def _task_md(**fields) -> list[str]:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines += [f"{key}:", *(f"  - {v}" for v in value)]
        else:
            lines.append(f"{key}: {value}")
    return [*lines, "---", "", "# t"]


class _Vault:
    """A tmp vault holding the three directories usage_value reads."""

    def __init__(self, root: Path):
        self.root = root
        self.runs = root / "Worker" / "runs"
        self.items = root / "Todo" / "items"
        self.assign = root / "Worker" / "assignments"
        for d in (self.runs, self.items, self.assign):
            d.mkdir(parents=True)

    def run(self, name: str, files: dict | None = None) -> Path:
        rd = self.runs / name
        rd.mkdir()
        for fname, value in (files or {}).items():
            (rd / fname).write_text(_content(value), encoding="utf-8")
        return rd

    def task(self, slug: str, md=None, ledger: dict | None = None) -> None:
        if md is not None:
            (self.items / f"{slug}.md").write_text(_content(md), encoding="utf-8")
        if ledger is not None:
            (self.assign / f"{slug}.json").write_text(json.dumps(ledger), encoding="utf-8")

    def install(self, *names: str) -> _Vault:
        for name in names:
            case = _case(name)
            for run in case["runs"]:
                self.run(run["name"], run["files"])
            for slug, task in case["tasks"].items():
                self.task(slug, task.get("md"), task.get("ledger"))
        return self

    def scan(self, now: dt.datetime = NOW):
        start = min(b[0] for b in uv.period_bounds(now).values())
        return uv.scan(self.runs, self.items, self.assign, start, now)

    def raw(self, now: dt.datetime = NOW) -> dict:
        runs, tasks = self.scan(now)
        return uv.aggregate(runs, tasks, now)

    def build(self, now: dt.datetime = NOW) -> dict:
        return uv.build(runs_dir=self.runs, items_dir=self.items, assign_dir=self.assign, now=now)


@pytest.fixture
def vault(tmp_path):
    uv.clear_cache()
    yield _Vault(tmp_path / "vault")
    uv.clear_cache()


def _count_loaders(monkeypatch) -> dict[str, list[str]]:
    """Wrap every file loader; the result maps loader name -> the paths it read."""
    seen: dict[str, list[str]] = {name: [] for name in _LOADERS}

    def wrap(name):
        real = getattr(uv, name)

        def counting(path):
            seen[name].append(str(path))
            return real(path)
        return counting

    for name in _LOADERS:
        monkeypatch.setattr(uv, name, wrap(name))
    return seen


@pytest.mark.parametrize("name", CASE_NAMES)
def test_each_real_shaped_case_gives_its_expected_rows(vault, name):
    case = _case(name)
    assert case["case"] == name
    assert case["source"].strip()
    vault.install(name)
    runs, tasks = vault.scan()
    assert len(runs) == len(case["runs"])
    assert _period(uv.aggregate(runs, tasks, NOW), "week")["rows"] == case["expect"]["week"]
    assert {slug: uv.classify(tasks[slug], slug)[1] for slug in tasks} == case["expect"]["rules"]


def test_all_cases_together_are_one_table(vault):
    vault.install(*CASE_NAMES)
    week = _period(vault.raw(), "week")
    assert [(r["project"], r["kind"], r["out_tokens"], r["runs"]) for r in week["rows"]] == [
        ("macp", "self", 466_000, 1), ("app", "discovery", 225_000, 2), ("app", "bug", 70_000, 3),
        ("vault", "other", 30_000, 1), ("app", "other", 12_000, 1)]
    assert week["total"] == {"out_tokens": 803_000, "runs": 8, "runs_unmetered": 3,
                             "merged_prs": 3, "done": 6, "cancelled": 0, "failed": 1,
                             "tokens_per_merge": 803_000 // 3}


def test_scan_sums_usage_of_a_real_shaped_five_stage_run(vault):
    vault.install("auto_five_stage_merged")
    name = "20261121T144046-engine-triage-cards"
    assert uv.run_usage(vault.runs / name) == (466_000, 5, 0)
    (run,), _ = vault.scan()
    assert (run["name"], run["slug"], run["at"]) == (
        name, "engine-triage-cards", dt.datetime(2026, 11, 21, 14, 40, 46))
    assert (run["out_tokens"], run["unmetered"]) == (466_000, False)


def test_scan_run_without_stages_json_is_unmetered(vault):
    vault.install("unmetered_runs")
    assert uv.run_usage(vault.runs / "20261119T090000-app-no-stages") is None
    assert uv.run_usage(vault.runs / "20261119T100000-app-no-usage") == (0, 0, 0)
    by_slug = {r["slug"]: r for r in vault.scan()[0]}
    for slug in ("app-no-stages", "app-no-usage"):
        assert (by_slug[slug]["out_tokens"], by_slug[slug]["unmetered"]) == (0, True), slug


def test_scan_unavailable_and_damaged_records_add_zero(vault):
    vault.install("unmetered_runs")
    # the real shape: a timed-out stage leaves {"unavailable": "timeout"}
    assert uv.run_usage(vault.runs / "20261120T154934-app-player-skip-wired") == (70_000, 3, 1)
    damaged = vault.run("20261121T100000-app-damaged", {"stages.json": {"usage": {
        "research": _usage(5_000),
        "plan": {**_usage(7_000), "output_tokens": "7000"},          # a string, not a count
        "implement": {k: v for k, v in _usage(9_000).items() if k != "input_tokens"},
        "verify": {**_usage(11_000), "output_tokens": True},         # a bool is not a count
        "report": "oops",
    }}})
    assert uv.run_usage(damaged) == (5_000, 1, 4)
    run = next(r for r in vault.scan()[0] if r["slug"] == "app-damaged")
    assert (run["out_tokens"], run["unmetered"]) == (5_000, True)


def test_scan_corrupt_stages_json_is_unmetered(vault):
    vault.run("20261120T100000-app-corrupt", {"stages.json": "{not json"})
    vault.run("20261120T110000-app-list", {"stages.json": "[1, 2]"})
    vault.run("20261120T120000-app-binary", {})
    (vault.runs / "20261120T120000-app-binary" / "stages.json").write_bytes(b"\xff\xfe\x00{")
    runs, _ = vault.scan()
    assert len(runs) == 3                                    # every run is still counted
    for run in runs:
        assert (run["out_tokens"], run["unmetered"]) == (0, True), run["name"]
    total = _period(vault.raw(), "week")["total"]
    assert (total["runs"], total["runs_unmetered"], total["out_tokens"]) == (3, 3, 0)


def test_scan_reads_pr_from_report_frontmatter_and_quick_body(vault):
    vault.install("auto_five_stage_merged", "quick_merged")
    assert uv.run_result(vault.runs / "20261121T144046-engine-triage-cards") == (
        "https://github.com/example/macp/pull/12", True)
    assert uv.run_result(vault.runs / "20261117T114215-app-doc-weekly-check") == (
        "https://github.com/example/app/pull/39", True)
    none = vault.run("20261120T100000-a", {"report.md": _report()})
    assert uv.run_result(none) == (None, None)
    open_pr = vault.run("20261120T100001-b", {"report.md": _report(
        '"https://github.com/example/app/pull/7/"', "no")})
    assert uv.run_result(open_pr) == ("https://github.com/example/app/pull/7", False)
    # report.md wins over quick.md, and a report's body is not searched for a PR line
    both = vault.run("20261120T100002-c", {
        "report.md": [*_report(), "- PR: https://github.com/example/app/pull/8"],
        "quick.md": ["- PR: https://github.com/example/app/pull/9", "- merged: yes"]})
    assert uv.run_result(both) == (None, None)
    no_pr = vault.run("20261120T100003-d", {"quick.md": ["---", "stage: quick", "---", "",
                                                         "- PR: 없음", "- merged: no — 0 commits"]})
    assert uv.run_result(no_pr) == (None, False)
    assert uv.run_result(vault.run("20261120T100004-e", {})) == (None, None)


def test_scan_delegate_run_has_no_pr(vault):
    vault.install("delegate_vault_done")
    assert uv.run_result(vault.runs / "20261122T140107-cost-review-20261121") == (None, None)
    # delegate.md and discover.md are never read for a PR, whatever they say
    rd = vault.run("20261120T100000-x", {
        "delegate.md": ["---", "status: pass", "pr_url: https://github.com/example/app/pull/99",
                        "merged: yes", "---"],
        "discover.md": ["- PR: https://github.com/example/app/pull/98", "- merged: yes"]})
    assert uv.run_result(rd) == (None, None)


def test_scan_ignores_non_run_names_and_plain_files(vault):
    vault.run("20261120T100000-app-real", {"stages.json": {"usage": {"quick": _usage(10)}}})
    # `…T250000-…` sorts inside the window as text but is not a time of day
    for name in ("notes", "2026-11-20-misc", "_state", "20261340T999999-bad-date",
                 "20261120T250000-bad-hour", "x-20261120T100000-app-real", "20261120T100000"):
        vault.run(name, {"stages.json": {"usage": {"quick": _usage(999)}}})
    (vault.runs / "20261120T110000-a-plain-file").write_text("not a run dir", encoding="utf-8")
    runs, tasks = vault.scan()
    assert [r["name"] for r in runs] == ["20261120T100000-app-real"]
    assert list(tasks) == ["app-real"]
    assert _period(vault.raw(), "week")["total"]["out_tokens"] == 10


def test_scan_slug_is_everything_after_the_timestamp(vault):
    vault.run("20261120T100000-engine-api-poc", {"stages.json": {"usage": {"quick": _usage(10)}}})
    for slug in ("poc", "api-poc", "engine-api-poc"):
        vault.task(slug, _task_md(status="open", repo=f"https://github.com/example/{slug}.git"))
    runs, tasks = vault.scan()
    assert [r["slug"] for r in runs] == ["engine-api-poc"]
    assert list(tasks) == ["engine-api-poc"]                 # `…-api-poc` is not a run of `poc`
    assert tasks["engine-api-poc"]["repo"] == "https://github.com/example/engine-api-poc.git"


def test_scan_lists_runs_dir_once_and_opens_nothing_outside_the_window(vault, monkeypatch):
    files = {"stages.json": {"usage": {"quick": _usage(10)}},
             "report.md": _report("https://github.com/example/app/pull/1", "yes")}
    vault.run("20261120T100000-app-in", files)
    vault.run("20261005T100000-app-october", files)          # before the month started
    vault.run("20261123T100000-app-tomorrow", files)         # after `now`
    for slug in ("app-in", "app-october", "app-tomorrow"):
        vault.task(slug, _task_md(status="done", repo=APP))
    seen = _count_loaders(monkeypatch)
    listed, stats = [], []
    real_scandir, real_stat = os.scandir, os.stat

    def scandir(path="."):
        listed.append(str(path))
        return real_scandir(path)

    def stat(path, *args, **kwargs):
        stats.append(str(path))
        return real_stat(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(os, "scandir", scandir)
        m.setattr(os, "stat", stat)
        runs, tasks = vault.scan()

    assert [r["slug"] for r in runs] == ["app-in"]
    assert listed == [str(vault.runs)]
    touched = stats + [p for paths in seen.values() for p in paths]
    assert any("app-in" in p for p in touched)
    for name in ("app-october", "app-tomorrow"):
        assert not [p for p in touched if name in p], name
    assert list(tasks) == ["app-in"]


def test_second_build_loads_no_file(vault, monkeypatch):
    vault.install(*CASE_NAMES)
    seen = _count_loaders(monkeypatch)
    first = vault.build()
    assert all(seen[name] for name in _LOADERS), seen        # every kind of file was read once
    for paths in seen.values():
        assert len(paths) == len(set(paths))
        paths.clear()
    assert vault.build() == first
    assert seen == {name: [] for name in _LOADERS}


def test_changed_file_is_reloaded_once(vault, monkeypatch):
    vault.install("auto_five_stage_merged", "quick_merged")
    first = vault.build()
    seen = _count_loaders(monkeypatch)
    rd = vault.runs / "20261117T114215-app-doc-weekly-check"
    (rd / "stages.json").write_text(json.dumps({"usage": {"quick": _usage(1_000_000)}}),
                                    encoding="utf-8")
    second = vault.build()
    assert seen == {**{name: [] for name in _LOADERS},
                    "_load_usage": [str(rd / "stages.json")]}
    assert first["periods"][0]["total"]["out_tokens"] == 478_000
    assert second["periods"][0]["total"]["out_tokens"] == 1_466_000
    assert second["periods"][0]["total"]["merged_prs"] == 2
    # a file that went away is forgotten: the run keeps counting, its PR does not
    (rd / "quick.md").unlink()
    total = vault.build()["periods"][0]["total"]
    assert (total["runs"], total["merged_prs"], total["done"]) == (2, 1, 2)
    (vault.items / "app-doc-weekly-check.md").unlink()       # no longer known to be done
    (vault.assign / "app-doc-weekly-check.json").unlink()
    total = vault.build()["periods"][0]["total"]
    assert (total["runs"], total["merged_prs"], total["done"]) == (2, 1, 1)


def test_task_facts_fall_back_to_ledger_payload_without_md(vault):
    row = {"task_id": "t", "state": "failed", "history": [],
           "payload": {"tags": ["todo", "feature"], "repo": APP, "worker_mode": "Auto"}}
    vault.task("t", ledger=row)
    facts = uv.task_facts("t", vault.items, vault.assign)
    assert facts == {"status": "", "tags": ["todo", "feature"], "repo": APP, "worker_mode": "auto",
                     "from_discovery": "", "discover_repo": "", "state": "failed", "deferred": False}
    assert (uv.project_of(facts, "t"), uv.classify(facts, "t"), uv.task_outcome(facts)) == (
        "app", ("feature", "tag:feature"), "failed")
    # a waiting row (checkout busy) is not a failure
    vault.task("busy", ledger={**row, "checkout_busy": True})
    assert uv.task_outcome(uv.task_facts("busy", vault.items, vault.assign)) is None
    # the task file wins where it speaks; the payload fills what it leaves out
    vault.task("both", _task_md(status="done", tags=["todo", "bug"]), ledger=row)
    facts = uv.task_facts("both", vault.items, vault.assign)
    assert (facts["status"], facts["tags"], facts["repo"], facts["worker_mode"]) == (
        "done", ["todo", "bug"], APP, "auto")
    assert uv.task_outcome(facts) == "done"
    # neither file: a task with no facts, which the slug rules still place
    facts = uv.task_facts("macp-gone", vault.items, vault.assign)
    assert (uv.project_of(facts, "macp-gone"), uv.classify(facts, "macp-gone")) == (
        "vault", ("self", "self_slug"))
    # a ledger file that is not a JSON object is read as no row
    (vault.assign / "junk.json").write_text("[]", encoding="utf-8")
    assert uv.task_facts("junk", vault.items, vault.assign)["state"] == ""


def test_task_file_placeholders_and_quotes_are_cleaned(vault):
    vault.task("t", ["---", "tags: [todo, Feature]", 'status: "Done"', "repo: '-'",
                     "worker_mode: off", 'from_discovery: "-"',
                     'discover_repo: "https://github.com/example/app.git"', "---", "", "# t"])
    facts = uv.task_facts("t", vault.items, vault.assign)
    assert facts == {"status": "done", "tags": ["todo", "feature"], "repo": "", "worker_mode": "off",
                     "from_discovery": "", "discover_repo": APP, "state": "", "deferred": False}


def test_missing_runs_dir_gives_empty_periods(tmp_path):
    uv.clear_cache()
    gone = tmp_path / "no-such-vault"
    model = uv.build(vault_root=gone, now=NOW)
    assert [p["id"] for p in model["periods"]] == ["week", "month"]
    for p in model["periods"]:
        assert p["rows"] == []
        assert (p["total"]["runs"], p["total"]["out_tokens"], p["total"]["tokens_per_merge"]) == (
            0, 0, None)
    assert not gone.exists()                                 # reading creates nothing


def test_build_resolves_the_three_dirs_under_vault_root(vault):
    vault.install(*CASE_NAMES)
    assert uv.build(vault_root=vault.root, now=NOW) == vault.build()
    assert uv.build(vault_root=str(vault.root), now=NOW) == vault.build()


def test_build_runs_no_subprocess(vault, monkeypatch):
    vault.install(*CASE_NAMES)

    def refuse(*args, **kwargs):
        raise AssertionError(f"subprocess started: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert vault.build()["periods"][0]["total"]["runs"] == 8


def test_build_writes_nothing(vault):
    vault.install(*CASE_NAMES)

    def snapshot() -> dict[str, tuple[int, int]]:
        return {str(p.relative_to(vault.root)): (p.stat().st_mtime_ns, p.stat().st_size)
                for p in sorted(vault.root.rglob("*"))}

    before = snapshot()
    vault.build()
    vault.build()
    assert snapshot() == before


def test_import_is_light(tmp_path):
    """Importing the module resolves no vault root and pulls in none of the
    engine's heavy modules: a clean process with no vault anywhere imports it."""
    code = ("import sys, todo_system.usage_value\n"
            "heavy = ('orchestrator', 'tasks', 'runner', 'stages', 'metrics', 'repos')\n"
            "print([m for m in heavy if 'todo_system.orchestrator.' + m in sys.modules])\n"
            "print('todo_system.session_log' in sys.modules)\n")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_REPO), "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-P", "-c", code], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n")[:2] == ["[]", "False"], r.stdout
