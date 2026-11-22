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
  `render_brief()` is a pure function of the same model, so both show the same
  numbers.

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

import datetime as dt
import re

from . import i18n

KINDS = ("discovery", "self", "feature", "bug", "security", "research", "ops", "other")
PERIODS = ("week", "month")
# The engine's own repo name. Compared with a repo NAME, never with a URL.
SELF_PROJECT = "macp"
# The project of a task with no repo: it runs in the vault itself.
VAULT_PROJECT = "vault"
WEEK_DAYS = 7

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
