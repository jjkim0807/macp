"""Usage vs results — what the worker's tokens bought, per project and kind of task.

The usage guard and the per-stage metrics say how much was spent. Neither says
on what, or what came back. This module answers that with one table: rows are
project x kind of task, columns are output tokens, runs, merged PRs, tasks
done / cancelled / failed and tokens per merge, for the last 7 days and for this
month. It reads local files only (run dirs, task files, ledger rows): no
network, no gh, no LLM, no subprocess. It writes nothing and changes no
dispatch or guard behaviour.

One wire model, two renderings:

* the dashboard: `scripts/macp_dashboard.py` `collect_usage_value()` puts
  `build()`'s result into `/api/status` as `usage_value`, and the web card and
  the Android app only render the strings in it;
* the brief: `scripts/brief.sh` calls `python -m todo_system.usage_value
  brief-section` after the agent wrote the brief and appends what it prints.
  `render_brief()` reads nothing but the same model, so both show the same
  numbers. The section goes into one brief a week: `brief_due()` skips it
  while a brief of the last 7 days carries BRIEF_MARKER. No state file, no
  switch.

CLI:

    python -m todo_system.usage_value brief-section [--vault-root DIR] [--now ISO]
    python -m todo_system.usage_value report [--vault-root DIR] [--now ISO] [--json] [--explain]

`brief-section` prints the section or nothing and always exits 0. `report`
prints every row; `--explain` adds what a person tuning the rules needs (rule
hits, the share of `other`, PR evidence, timings).

Wire model (source of truth; docs/dashboard-parity.md "Usage vs results"
restates it for the app). Additive: when `/api/status` has no `usage_value`
key, a client draws no card.

    {"title": str, "basis": str,
     "periods": [{"id": "week" | "month", "label": str, "range": "11/16–11/22",
                  "start": "2026-11-16", "end": "2026-11-22",
                  "total": CELL,
                  "rows": [{"project": str, "kind": str, "kind_label": str, **CELL}]}]}

    CELL = {"out_tokens": int, "out_tokens_text": "4.1M",
            "runs": int, "runs_unmetered": int, "merged_prs": int,
            "done": int, "cancelled": int, "failed": int,
            "tokens_per_merge": int | None, "tokens_per_merge_text": "292k" | "—",
            "text": str}

* `periods` is always `week` then `month`. A row is a (project, kind) with at
  least one run in that period; rows are ordered by output tokens (high first),
  then runs (high first), then project, then kind.
* `kind` is a stable id from KINDS. `project` is a data value (a repo name, or
  `vault`) and is never translated. Every other string is in the server's UI
  language (`i18n.t` at call time), numbers already formatted.
* `tokens_per_merge` is `out_tokens // merged_prs`, or None with no merge.
* `text` is the cell as one line; it names `runs_unmetered` only when > 0.

Counting rules

| value          | rule |
|----------------|------|
| run            | a directory under Worker/runs named `<YYYYMMDDThhmmss>-<slug>`. The time and the slug come from the name alone (the runner host's local clock; the slug is everything after the timestamp). A period is `[start, now]`, both ends included. |
| out_tokens     | sum of `output_tokens` over the `usage.<stage>` records of the run's stages.json that `metrics._usage_counted` accepts. Any other record adds 0. |
| runs_unmetered | runs with no accepted record at all (no stages.json, a corrupt one, no `usage`), or with at least one record that was not accepted (`unavailable`, damaged). Their tokens are missing, so `out_tokens` is a lower bound. |
| merged_prs     | per task: the PRs of its runs that say `merged: yes`, plus, when the task is done, the PR of its LAST run in the period that names one. Distinct URLs. The total counts distinct URLs over the whole period. |
| PR / merged    | read from the run's `report.md` (frontmatter `pr_url:`, `merged:`), else `quick.md` (`- PR:` line, `merged:`). Never checked against GitHub. |
| done / cancelled / failed | distinct tasks with a run in the period, by their state NOW: done = task file `status: done`; cancelled = `cancelled` / `canceled`; failed = the task file is not closed and the ledger row is `failed` and not deferred. A parked (`awaiting_human`) task is none of the three. |

What this cannot see: runs of a remote runner and run dirs already pruned (the
run dir stays on the machine that ran it); the PR of a vault delegate run (the
runner opens it after the stage and its URL is in no run dir — such a task
shows up as done only); usage of the planner, the brief and interactive
sessions. A done task whose last PR was closed without a merge is counted as
one merge too many.

Project key (first match): a discovery run -> the repo name of the task file's
`discover_repo`, else the slug before `-discovery-<YYYYMMDD>`; else the repo
name of the task file's `repo`, else of the ledger payload's; else `vault`.
The repo name is the last path segment of the URL without `.git`, lowercased
and slug-safe. No URL with an owner is ever compared.

Kind (first match; `classify` returns `(kind, rule_id)`):

| # | kind      | when                                                          | rule_id          |
|---|-----------|---------------------------------------------------------------|------------------|
| 1 | discovery | the task file has `from_discovery`                            | `from_discovery` |
| 1 | discovery | `worker_mode: discover`, or the slug ends `-discovery-<YYYYMMDD>` | `discover_run` |
| 2 | self      | the project key is SELF_PROJECT                               | `self_repo`      |
| 2 | self      | a tag is SELF_PROJECT                                         | `self_tag`       |
| 2 | self      | the slug starts with `<SELF_PROJECT>-`                        | `self_slug`      |
| 3 | by tag    | the first entry of _TAG_KINDS whose tag the task carries      | `tag:<tag>`      |
| 4 | by slug   | the first entry of _SLUG_PREFIX_KINDS the slug starts with    | `slug:<prefix>`  |
| 5 | other     | nothing matched                                               | `none`           |

So a security fix in the engine repo (repo, `security` tag and `security-fix-`
prefix all at once) is `self`: `security` means security work in other
projects.

Import stays as light as paths and i18n: the engine modules that resolve a
vault root at import (stages, metrics, session_log) are imported inside the
functions that need them, so `--vault-root` can set the root first.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

from . import i18n, paths

KINDS = ("discovery", "self", "feature", "bug", "security", "research", "ops", "other")
PERIODS = ("week", "month")
# The engine's own repo name. Compared with a repo NAME, never with a URL.
SELF_PROJECT = "macp"
# The project of a task with no repo: it runs in the vault itself.
VAULT_PROJECT = "vault"
WEEK_DAYS = 7
# The brief shows this many rows per period; the dashboard shows them all.
BRIEF_TOP_ROWS = 5
# First line of the brief section. brief_due() looks for it (never for the
# heading, which is translated) to put the table into one brief a week.
BRIEF_MARKER = "<!-- macp:usage-value -->"
_COLUMNS = ("row", "out", "runs", "merged", "status", "per_merge")
_EXPLAIN_TAGS = 15
_EXPLAIN_TASKS = 10

# Rule 3, in this order: the first entry whose tag the task carries decides.
_TAG_KINDS = (
    ("security", "security"),
    ("bug", "bug"), ("bugfix", "bug"), ("fix", "bug"), ("regression", "bug"),
    ("research", "research"), ("experiment", "research"), ("paper", "research"),
    ("infra", "ops"), ("ops", "ops"), ("maintenance", "ops"), ("admin", "ops"),
    ("feature", "feature"),
)
# Rule 4, in this order: the first prefix the slug starts with decides.
_SLUG_PREFIX_KINDS = (
    ("security-fix-", "security"), ("security-", "security"),
    ("fix-", "bug"), ("bug-", "bug"), ("bugfix-", "bug"), ("hotfix-", "bug"),
    ("research-", "research"),
    ("ops-", "ops"), ("infra-", "ops"),
    ("feat-", "feature"), ("feature-", "feature"),
)

_RUN_NAME_RE = re.compile(r"^(\d{8}T\d{6})-(.+)$")
_RUN_TS_FORMAT = "%Y%m%dT%H%M%S"
_DISCOVERY_SLUG_RE = re.compile(r"^(.+)-discovery-\d{8}$")
_CANCELLED = ("cancelled", "canceled")
_COUNT_KEYS = ("out_tokens", "runs", "runs_unmetered", "done", "cancelled", "failed")
_NO_VALUE = "—"


# --- periods ---------------------------------------------------------------------

def _local_naive(now: dt.datetime) -> dt.datetime:
    """`now` as a naive local time, the clock run dir names are written in."""
    return now.astimezone().replace(tzinfo=None) if now.tzinfo is not None else now


def period_bounds(now: dt.datetime) -> dict[str, tuple[dt.datetime, dt.datetime]]:
    """`{"week": (start, end), "month": (start, end)}` in naive local time.

    Both end at `now`. The week starts at 00:00 six days before today, so it
    covers WEEK_DAYS calendar days; the month starts at 00:00 on the 1st."""
    now = _local_naive(now)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {"week": (midnight - dt.timedelta(days=WEEK_DAYS - 1), now),
            "month": (midnight.replace(day=1), now)}


def parse_run_name(name: str) -> tuple[dt.datetime, str] | None:
    """`(start time, slug)` of a run dir name, or None when it is not one.

    The slug is everything after the `<timestamp>-` prefix, so `…-api-poc` is
    a run of `api-poc`, never of `poc`."""
    m = _RUN_NAME_RE.match(name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), _RUN_TS_FORMAT), m.group(2)
    except ValueError:
        return None


# --- task facts -> project and kind --------------------------------------------------

def _text(value) -> str:
    """A frontmatter scalar as text; "" for a missing value or the `-` placeholder."""
    if not isinstance(value, str):
        return ""
    value = value.strip().strip("\"'").strip()
    return "" if value == "-" else value


def _tags(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = [str(t) for t in value]
    else:
        items = str(value or "").split(",")
    return [t.strip().strip("\"'").lower() for t in items if t.strip()]


def project_name(url: str) -> str:
    """Slug-safe repo name of a git URL: `…/DripCoffee.git` -> `dripcoffee`.

    A copy of discovery.project_name (and repos.repo_name under it): importing
    discovery pulls in repos and resolves a vault root at import, which this
    module must not do. test_usage_value pins the two together."""
    name = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
    if name.endswith(".git"):
        name = name[:-4]
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"


def _is_discovery_run(task: dict, slug: str) -> bool:
    return (_text(task.get("worker_mode")).lower() == "discover"
            or _DISCOVERY_SLUG_RE.match(slug) is not None)


def project_of(task: dict | None, slug: str) -> str:
    """The row's project for a task: a repo name, or VAULT_PROJECT (see the
    module docstring, "Project key")."""
    task = task or {}
    if _is_discovery_run(task, slug):
        url = _text(task.get("discover_repo"))
        if url:
            return project_name(url)
        m = _DISCOVERY_SLUG_RE.match(slug)
        if m:
            return m.group(1).lower()
    url = _text(task.get("repo"))
    return project_name(url) if url else VAULT_PROJECT


def classify(task: dict | None, slug: str) -> tuple[str, str]:
    """`(kind, rule_id)` for a task: the first rule that matches (see the module
    docstring, "Kind"). `task` holds the facts task_facts() reads; None or {}
    for a task with no file left, which leaves only the slug rules."""
    task = task or {}
    if _text(task.get("from_discovery")):
        return "discovery", "from_discovery"
    if _is_discovery_run(task, slug):
        return "discovery", "discover_run"
    tags = _tags(task.get("tags"))
    if project_of(task, slug) == SELF_PROJECT:
        return "self", "self_repo"
    if SELF_PROJECT in tags:
        return "self", "self_tag"
    if slug.startswith(SELF_PROJECT + "-"):
        return "self", "self_slug"
    for tag, kind in _TAG_KINDS:
        if tag in tags:
            return kind, f"tag:{tag}"
    for prefix, kind in _SLUG_PREFIX_KINDS:
        if slug.startswith(prefix):
            return kind, f"slug:{prefix}"
    return "other", "none"


def task_outcome(task: dict | None) -> str | None:
    """`done`, `cancelled` or `failed` for a task's state now, else None."""
    task = task or {}
    status = _text(task.get("status")).lower()
    if status == "done":
        return "done"
    if status in _CANCELLED:
        return "cancelled"
    if task.get("state") == "failed" and not task.get("deferred"):
        return "failed"
    return None


# --- aggregation ---------------------------------------------------------------------

def _pr_key(url) -> str:
    return str(url or "").strip().rstrip("/")


def merged_pr_urls(runs: list[dict], task: dict | None) -> set[str]:
    """The merged PRs of ONE task's runs (oldest first) in one period.

    Every PR a run reports as `merged: yes`, plus the PR of the last run that
    names one when the task is done now: a PR merged by a person after the run
    parked reads `merged: no` in its report forever. Only the last one, so the
    closed PR of an earlier attempt is not counted."""
    urls = {_pr_key(r.get("pr_url")) for r in runs if r.get("merged") is True}
    if task_outcome(task) == "done":
        named = [_pr_key(r.get("pr_url")) for r in runs if _pr_key(r.get("pr_url"))]
        if named:
            urls.add(named[-1])
    urls.discard("")
    return urls


def _cell(counts: dict, urls: set[str]) -> dict:
    merged = len(urls)
    return {"out_tokens": counts["out_tokens"], "runs": counts["runs"],
            "runs_unmetered": counts["runs_unmetered"], "merged_prs": merged,
            "done": counts["done"], "cancelled": counts["cancelled"],
            "failed": counts["failed"],
            "tokens_per_merge": counts["out_tokens"] // merged if merged else None}


def _aggregate_period(pid: str, start: dt.datetime, end: dt.datetime,
                      runs: list[dict], tasks: dict) -> dict:
    by_slug: dict[str, list[dict]] = {}
    for run in sorted(runs, key=lambda r: (r["at"], r.get("name") or "")):
        if start <= run["at"] <= end:
            by_slug.setdefault(run["slug"], []).append(run)
    counts: dict[tuple[str, str], dict] = {}
    urls: dict[tuple[str, str], set[str]] = {}
    for slug, slug_runs in by_slug.items():
        task = tasks.get(slug) or {}
        key = (project_of(task, slug), classify(task, slug)[0])
        c = counts.setdefault(key, dict.fromkeys(_COUNT_KEYS, 0))
        c["runs"] += len(slug_runs)
        c["out_tokens"] += sum(int(r.get("out_tokens") or 0) for r in slug_runs)
        c["runs_unmetered"] += sum(1 for r in slug_runs if r.get("unmetered"))
        outcome = task_outcome(task)
        if outcome:
            c[outcome] += 1
        urls.setdefault(key, set()).update(merged_pr_urls(slug_runs, task))
    rows = [{"project": key[0], "kind": key[1], **_cell(c, urls[key])}
            for key, c in counts.items()]
    rows.sort(key=lambda r: (-r["out_tokens"], -r["runs"], r["project"], r["kind"]))
    total = {k: sum(r[k] for r in rows) for k in _COUNT_KEYS}
    return {"id": pid, "start": start.date().isoformat(), "end": end.date().isoformat(),
            "total": _cell(total, set().union(*urls.values())), "rows": rows}


def aggregate(runs: list[dict], tasks: dict, now: dt.datetime) -> dict:
    """The table as numbers only: `{"periods": [week, month]}`.

    Pure. `runs` are records `{"name", "slug", "at", "out_tokens", "unmetered",
    "pr_url", "merged"}` (scan() builds them; `at` is the naive local time from
    the run dir name). `tasks` maps a slug to the facts task_facts() reads; a
    slug that is missing counts as a task with no file. present() turns the
    result into the wire model."""
    bounds = period_bounds(now)
    return {"periods": [_aggregate_period(pid, *bounds[pid], runs, tasks) for pid in PERIODS]}


# --- presentation: the wire model ----------------------------------------------------------

def _scaled(n: int, size: int, unit: str, *, last: bool) -> str | None:
    tenths = (n * 10 + size // 2) // size
    if tenths < 1000:
        return f"{tenths // 10}.{tenths % 10}{unit}"
    whole = (n + size // 2) // size
    return f"{whole}{unit}" if whole < 1000 or last else None


def fmt_tokens(n: int | None) -> str:
    """A token count in three significant figures or fewer: `950`, `1.5k`,
    `205k`, `4.1M`, `123M`; `—` for None."""
    if n is None:
        return _NO_VALUE
    n = int(n)
    if n < 1000:
        return str(n)
    return _scaled(n, 1_000, "k", last=False) or _scaled(n, 1_000_000, "M", last=True)


def _range_text(start: str, end: str) -> str:
    a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    return f"{a.month}/{a.day}–{b.month}/{b.day}"


def _present_cell(cell: dict) -> dict:
    per_merge = fmt_tokens(cell["tokens_per_merge"])
    text = i18n.t("usage_value.cell", runs=cell["runs"], merged=cell["merged_prs"],
                  done=cell["done"], cancelled=cell["cancelled"], failed=cell["failed"],
                  per_merge=per_merge)
    if cell["runs_unmetered"]:
        text += i18n.t("usage_value.cell_unmetered", n=cell["runs_unmetered"])
    return {"out_tokens": cell["out_tokens"], "out_tokens_text": fmt_tokens(cell["out_tokens"]),
            "runs": cell["runs"], "runs_unmetered": cell["runs_unmetered"],
            "merged_prs": cell["merged_prs"], "done": cell["done"],
            "cancelled": cell["cancelled"], "failed": cell["failed"],
            "tokens_per_merge": cell["tokens_per_merge"], "tokens_per_merge_text": per_merge,
            "text": text}


def present(raw: dict) -> dict:
    """aggregate()'s numbers as the wire model: titles, labels, formatted
    numbers and one-line cells added, in the UI language of this call."""
    periods = []
    for p in raw["periods"]:
        rows = [{"project": r["project"], "kind": r["kind"],
                 "kind_label": i18n.t(f"usage_value.kind.{r['kind']}"), **_present_cell(r)}
                for r in p["rows"]]
        periods.append({"id": p["id"], "label": i18n.t(f"usage_value.period.{p['id']}"),
                        "range": _range_text(p["start"], p["end"]),
                        "start": p["start"], "end": p["end"],
                        "total": _present_cell(p["total"]), "rows": rows})
    return {"title": i18n.t("usage_value.title"), "basis": i18n.t("usage_value.basis"),
            "periods": periods}


# --- reading the files -------------------------------------------------------------------

_RESULT_HEAD = 8_000      # chars of report.md / quick.md read: the frontmatter and the summary lines
_TASK_HEAD = 65_536       # chars of a task file read: its frontmatter sits at the top
_PR_URL_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/\d+")
_BODY_PR_RE = re.compile(r"^- PR:[ \t]*(.+)$", re.M)

# path -> (mtime_ns, size, parsed value). The dashboard rebuilds the table every
# few seconds over files that almost never change, so each is parsed once per
# change and a rebuild costs one stat per file. Bounded by a blunt clear.
_CACHE: dict[str, tuple[int, int, object]] = {}
_CACHE_MAX = 20_000


def clear_cache() -> None:
    _CACHE.clear()


def _cached(path: str, load):
    """`load(path)`, run again only when the file's mtime or size changed. None
    for a missing file. A loader returns None for a file it cannot use."""
    try:
        st = os.stat(path)
    except OSError:
        _CACHE.pop(path, None)
        return None
    hit = _CACHE.get(path)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    value = load(path)
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[path] = (st.st_mtime_ns, st.st_size, value)
    return value


def _read_head(path: str, limit: int) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return None


def _load_usage(path: str) -> tuple[int, int, int] | None:
    from .orchestrator import metrics    # resolves a vault root at import: not at module top
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    out = counted = uncounted = 0
    for rec in (usage.values() if isinstance(usage, dict) else ()):
        if metrics._usage_counted(rec):   # the one definition of a metered record, shared
            out += rec["output_tokens"]
            counted += 1
        else:
            uncounted += 1
    return out, counted, uncounted


def _result_of(text: str | None, *, body_pr: bool) -> tuple[str | None, bool | None] | None:
    from .orchestrator import stages     # resolves a vault root at import: not at module top
    if text is None:
        return None
    url = stages.parse_report_pr_url(text)
    if url is not None:
        m = _PR_URL_RE.search(url)
        url = m.group(0) if m else _pr_key(url) or None
    elif body_pr:
        line = _BODY_PR_RE.search(text)
        m = _PR_URL_RE.search(line.group(1)) if line else None
        url = m.group(0) if m else None
    return url, stages.parse_report_merged(text)


def _load_report(path: str):
    return _result_of(_read_head(path, _RESULT_HEAD), body_pr=False)


def _load_quick(path: str):
    return _result_of(_read_head(path, _RESULT_HEAD), body_pr=True)


def _load_task(path: str) -> dict | None:
    from .session_log import parse_frontmatter   # resolves a vault root at import
    text = _read_head(path, _TASK_HEAD)
    if text is None:
        return None
    fm, _ = parse_frontmatter(text)
    return {"status": _text(fm.get("status")).lower(), "tags": _tags(fm.get("tags")),
            "repo": _text(fm.get("repo")), "worker_mode": _text(fm.get("worker_mode")).lower(),
            "from_discovery": _text(fm.get("from_discovery")),
            "discover_repo": _text(fm.get("discover_repo"))}


def _load_ledger(path: str) -> dict | None:
    from .orchestrator import assignment
    try:
        with open(path, encoding="utf-8") as fh:
            row = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(row, dict):
        return None
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return {"state": str(row.get("state") or ""), "deferred": assignment.is_deferred(row),
            "tags": _tags(payload.get("tags")), "repo": _text(payload.get("repo")),
            "worker_mode": _text(payload.get("worker_mode")).lower()}


def run_usage(run_dir) -> tuple[int, int, int] | None:
    """`(output tokens, accepted records, other records)` of a run's stages.json.

    None when the file is missing or is not a JSON object; `(0, 0, 0)` when it
    has no `usage`. A record counts when metrics._usage_counted accepts it."""
    return _cached(os.path.join(run_dir, "stages.json"), _load_usage)


def run_result(run_dir) -> tuple[str | None, bool | None]:
    """`(PR url, merged)` a run reports: report.md if the run wrote one, else
    quick.md. `(None, None)` when it wrote neither; delegate.md and discover.md
    carry no PR and are not read."""
    for name, load in (("report.md", _load_report), ("quick.md", _load_quick)):
        found = _cached(os.path.join(run_dir, name), load)
        if found is not None:
            return found
    return None, None


def task_facts(slug: str, items_dir, assign_dir) -> dict:
    """What project_of(), classify() and task_outcome() read about one task.

    `status`, `from_discovery` and `discover_repo` come from the task file only;
    `tags`, `repo` and `worker_mode` fall back to the ledger payload when the
    file has none (or is gone); `state` and `deferred` are the ledger row's.
    Missing values are "" / [] / False."""
    md = _cached(os.path.join(items_dir, f"{slug}.md"), _load_task) or {}
    row = _cached(os.path.join(assign_dir, f"{slug}.json"), _load_ledger) or {}
    return {"status": md.get("status") or "",
            "tags": md.get("tags") or row.get("tags") or [],
            "repo": md.get("repo") or row.get("repo") or "",
            "worker_mode": md.get("worker_mode") or row.get("worker_mode") or "",
            "from_discovery": md.get("from_discovery") or "",
            "discover_repo": md.get("discover_repo") or "",
            "state": row.get("state") or "", "deferred": bool(row.get("deferred"))}


def scan(runs_dir, items_dir, assign_dir, start: dt.datetime,
         end: dt.datetime) -> tuple[list[dict], dict[str, dict]]:
    """`(run records, {slug: task facts})` for the runs named inside [start, end].

    `runs_dir` is listed once. A name outside the window is dropped on the name
    alone: nothing of that run is opened or stat-ed. ([], {}) when the
    directory cannot be listed."""
    lo, hi = start.strftime(_RUN_TS_FORMAT), end.strftime(_RUN_TS_FORMAT)
    runs: list[dict] = []
    try:
        entries = os.scandir(runs_dir)
    except OSError:
        return runs, {}
    with entries:
        for entry in entries:
            m = _RUN_NAME_RE.match(entry.name)
            if m is None or not lo <= m.group(1) <= hi:
                continue
            parsed = parse_run_name(entry.name)
            try:
                if parsed is None or not entry.is_dir():
                    continue
            except OSError:
                continue
            usage = run_usage(entry.path)
            pr_url, merged = run_result(entry.path)
            runs.append({"name": entry.name, "slug": parsed[1], "at": parsed[0],
                         "out_tokens": usage[0] if usage else 0,
                         "unmetered": usage is None or usage[1] == 0 or usage[2] > 0,
                         "pr_url": pr_url, "merged": merged})
    runs.sort(key=lambda r: r["name"])
    tasks = {slug: task_facts(slug, items_dir, assign_dir)
             for slug in sorted({r["slug"] for r in runs})}
    return runs, tasks


def _vault(vault_root) -> Path:
    return (Path(vault_root) if vault_root is not None
            else paths.vault_root(start=paths.vault_search_start()))


def _dirs(vault_root, runs_dir, items_dir, assign_dir) -> tuple:
    """(runs, items, assignments): each as given, else its standard place in the vault."""
    if runs_dir is None or items_dir is None or assign_dir is None:
        root = _vault(vault_root)
        runs_dir = runs_dir if runs_dir is not None else root / "Worker" / "runs"
        items_dir = items_dir if items_dir is not None else root / "Todo" / "items"
        assign_dir = assign_dir if assign_dir is not None else root / "Worker" / "assignments"
    return runs_dir, items_dir, assign_dir


def build(vault_root=None, *, runs_dir=None, items_dir=None, assign_dir=None,
          now: dt.datetime | None = None) -> dict:
    """The wire model (module docstring) for the vault's run dirs, as of `now`.

    The three directories default to the standard places under `vault_root`
    (itself defaulting to the resolved vault). Reads local files only, cached
    per file; a missing directory gives two empty periods."""
    dirs = _dirs(vault_root, runs_dir, items_dir, assign_dir)
    now = _local_naive(now if now is not None else dt.datetime.now())
    start = min(b[0] for b in period_bounds(now).values())
    runs, tasks = scan(*dirs, start, now)
    return present(aggregate(runs, tasks, now))


# --- the brief section ---------------------------------------------------------------------

def _md_row(label: str, cell: dict) -> str:
    runs = str(cell["runs"])
    if cell["runs_unmetered"]:
        runs += i18n.t("usage_value.cell_unmetered", n=cell["runs_unmetered"])
    return (f"| {label} | {cell['out_tokens_text']} | {runs} | {cell['merged_prs']} | "
            f"{cell['done']}/{cell['cancelled']}/{cell['failed']} | "
            f"{cell['tokens_per_merge_text']} |")


def render_markdown(model: dict, *, top: int | None = None, marker: bool = False) -> str:
    """The wire model as a markdown section (no trailing newline): a heading,
    the basis line, then per period a table of its rows and a total row.

    `top` keeps the first N rows of each period and says how many were left
    out; the total row is always the whole period. `marker` puts BRIEF_MARKER
    on the first line. Reads nothing but `model`; the column headings and the
    few fixed words come from i18n in the UI language of this call. No line
    starts with `>` and the only heading is this section's own, so the brief
    notification's parser (scripts/_build_brief_message.py) never reads it."""
    head = "| " + " | ".join(i18n.t(f"usage_value.col.{c}") for c in _COLUMNS) + " |"
    lines = [BRIEF_MARKER] if marker else []
    lines += [f"## 📈 {model['title']}", "", f"_{model['basis']}_"]
    for p in model["periods"]:
        lines += ["", f"**{p['label']}** ({p['range']})", ""]
        rows = p["rows"]
        if not rows:
            lines.append(i18n.t("usage_value.no_runs"))
            continue
        shown = rows if top is None else rows[:top]
        lines += [head, "|---|---:|---:|---:|---:|---:|"]
        lines += [_md_row(f"{r['project']} · {r['kind_label']}", r) for r in shown]
        lines.append(_md_row(f"**{i18n.t('usage_value.total')}**", p["total"]))
        if len(rows) > len(shown):
            lines += ["", i18n.t("usage_value.more_rows", n=len(rows) - len(shown))]
    return "\n".join(lines)


def render_brief(model: dict) -> str:
    """The section brief.sh appends to the brief: the top BRIEF_TOP_ROWS rows
    of each period, led by BRIEF_MARKER."""
    return render_markdown(model, top=BRIEF_TOP_ROWS, marker=True)


def brief_due(briefings_dir, today: dt.date) -> bool:
    """True when no brief of the last WEEK_DAYS days (today included) carries
    BRIEF_MARKER — the table goes into one brief a week.

    The briefs themselves are the record: there is no state file and no fixed
    weekday, so a day without a brief just moves the table to the next one, and
    a brief the agent rewrote without the table gets it back on the next run."""
    for back in range(WEEK_DAYS):
        day = today - dt.timedelta(days=back)
        try:
            with open(os.path.join(briefings_dir, f"{day.isoformat()}.md"),
                      encoding="utf-8", errors="replace") as fh:
                if BRIEF_MARKER in fh.read():
                    return False
        except OSError:
            continue
    return True


def brief_section(vault_root=None, *, now: dt.datetime | None = None) -> str:
    """render_brief() of the vault's table when one is due, else "".

    "" as well when neither period has a run: an empty table is not worth a
    section."""
    root = _vault(vault_root)
    now = _local_naive(now if now is not None else dt.datetime.now())
    if not brief_due(root / "Todo" / "Briefings", now.date()):
        return ""
    model = build(root, now=now)
    if not any(p["rows"] for p in model["periods"]):
        return ""
    return render_brief(model)


# --- diagnostics and the CLI ------------------------------------------------------------------

def explain(vault_root=None, *, runs_dir=None, items_dir=None, assign_dir=None,
            now: dt.datetime | None = None) -> str:
    """This month's table taken apart, for a person tuning the rules: how many
    tasks each rule id caught, what share of the output tokens fell to `other`
    (with its commonest tags and largest tasks), how the merged PRs were
    evidenced, and how long a cold build and a rebuild took. Names real slugs,
    so it is printed for the operator and never part of the wire model."""
    dirs = _dirs(vault_root, runs_dir, items_dir, assign_dir)
    now = _local_naive(now if now is not None else dt.datetime.now())
    clear_cache()                      # so "first build" is a cold one
    t0 = time.perf_counter()
    build(runs_dir=dirs[0], items_dir=dirs[1], assign_dir=dirs[2], now=now)
    t1 = time.perf_counter()
    build(runs_dir=dirs[0], items_dir=dirs[1], assign_dir=dirs[2], now=now)
    t2 = time.perf_counter()

    start, end = period_bounds(now)["month"]
    runs, tasks = scan(*dirs, start, end)
    by_slug: dict[str, list[dict]] = {}
    for run in runs:
        by_slug.setdefault(run["slug"], []).append(run)
    kinds = {slug: classify(tasks.get(slug), slug) for slug in by_slug}
    tokens = {slug: sum(r["out_tokens"] for r in slug_runs) for slug, slug_runs in by_slug.items()}
    total = sum(tokens.values())
    other = sorted((s for s in by_slug if kinds[s][0] == "other"), key=lambda s: (-tokens[s], s))
    other_tokens = sum(tokens[s] for s in other)

    def by_count(counter: Counter) -> list[tuple[str, int]]:
        return sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))

    lines = [f"explain: this month {start.date().isoformat()}..{end.date().isoformat()} — "
             f"{len(runs)} run(s) of {len(by_slug)} task(s)",
             "rule hits (tasks):"]
    lines += [f"  {rule} {n}" for rule, n in by_count(Counter(rule for _, rule in kinds.values()))]
    share = 100 * other_tokens / total if total else 0.0
    lines.append(f"other: {share:.1f}% of this month's output tokens "
                 f"({fmt_tokens(other_tokens)} of {fmt_tokens(total)}) in {len(other)} task(s)")
    if other:
        tags = Counter(t for s in other for t in (tasks.get(s) or {}).get("tags") or [])
        lines.append("  tags on other tasks: "
                     + ", ".join(f"{t} {n}" for t, n in by_count(tags)[:_EXPLAIN_TAGS]))
        lines.append("  top other tasks by output tokens:")
        for slug in other[:_EXPLAIN_TASKS]:
            facts = tasks.get(slug) or {}
            lines.append(f"    {fmt_tokens(tokens[slug])}  {len(by_slug[slug])} run(s)  "
                         f"{project_of(facts, slug)}  {slug}  [{', '.join(facts.get('tags') or [])}]")
    said = {slug: {_pr_key(r["pr_url"]) for r in slug_runs if r["merged"] is True}
            for slug, slug_runs in by_slug.items()}
    done_only = sum(len(merged_pr_urls(slug_runs, tasks.get(slug)) - said[slug])
                    for slug, slug_runs in by_slug.items())
    lines.append(f"PR evidence: {sum(1 for r in runs if r['pr_url'])} run(s) name a PR, "
                 f"{sum(1 for r in runs if r['merged'] is True)} say merged: yes, "
                 f"{done_only} PR(s) counted through the done rule only")
    lines.append(f"timing: first build {t1 - t0:.3f}s, rebuild {t2 - t1:.3f}s")
    return "\n".join(lines)


def _parse_now(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value else None


def main(argv: list[str] | None = None) -> int:
    """`brief-section` prints the section for today's brief, or nothing; it
    always exits 0, and any error is one stderr line (brief.sh appends the
    stdout as it is and must never lose a brief over this). `report` prints the
    whole table (`--json`: the wire model; `--explain`: the diagnostics after
    it) and lets errors show."""
    parser = argparse.ArgumentParser(
        prog="python -m todo_system.usage_value",
        description="Usage vs results: output tokens, runs and merged PRs per project and "
                    "kind of task, from the vault's local run dirs.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    brief = sub.add_parser("brief-section",
                           help="print the weekly brief section (nothing when not due or on error)")
    report = sub.add_parser("report", help="print the whole table")
    for p in (brief, report):
        p.add_argument("--vault-root", default=None,
                       help="read this vault instead of the resolved one")
        p.add_argument("--now", default=None, help="compute as of this ISO time (default: now)")
    report.add_argument("--json", action="store_true", help="print the wire model as JSON")
    report.add_argument("--explain", action="store_true",
                        help="add rule hits, the share of `other`, PR evidence and timings")
    args = parser.parse_args(argv)
    if args.vault_root:
        # Before anything else is imported: the engine modules build() borrows
        # resolve the vault root when they load, and the UI language is read
        # from that vault's config.
        os.environ["MACP_VAULT_ROOT"] = os.path.abspath(os.path.expanduser(args.vault_root))
    if args.cmd == "brief-section":
        try:
            text = brief_section(args.vault_root, now=_parse_now(args.now))
            if text:
                sys.stdout.write(text + "\n")
        except Exception as exc:  # noqa: BLE001 - fail safe: print nothing, exit 0
            print(f"usage-value: error ({type(exc).__name__})", file=sys.stderr)
        return 0
    now = _parse_now(args.now)
    model = build(args.vault_root, now=now)
    print(json.dumps(model, ensure_ascii=False, indent=1) if args.json else render_markdown(model))
    if args.explain:
        print("\n" + explain(args.vault_root, now=now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
