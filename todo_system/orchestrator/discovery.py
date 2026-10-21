"""Planner-fed project discovery: find work by RUNNING and RE-READING a project.

The planner (worker-planner.md) only ever extends what a task file already
says: the next defined leaf, a red CI, a `TODO` it happened to grep. When every
leaf is done, blocked or human-owned it answers `exhausted` and the queue goes
quiet — even though the projects themselves are full of things a fresh pair of
eyes would find in an afternoon: a smoke launch that fails, a module nobody
tested, a README that describes commands that no longer exist.

This module turns that afternoon into a scheduled worker run. When the planner
is exhausted, `maybe-create` writes ONE `worker_mode: discover` task for the
active project whose last discovery is oldest (never-explored first). The runner
executes it as the single `discover` stage (worker-discover.md): clone the repo
into /tmp, run its own tests/lint/build and a bounded smoke launch, sweep the
whole tree, and write a candidate-task report to
`Memory/episodes/<date>-discovery-<name>.md` (git-tracked, dirty-guard exempt,
auto-committed by sync_vault_state — no PR needed). The planner tick then injects
every unconsumed report into the next planner run (`emit-context`), and the
planner creates real tasks stamped `from_discovery: <report-stem>` or rejects
the report in `discovery_rejected.jsonl`; `reconcile` closes what it handled and
expires a report the planner ignored MAX_INJECTIONS times.

Cost is bounded three ways: only when the planner is exhausted (nothing better
to do), at most one open discovery task at a time, and one per repo per
`interval_days` (default 14, `MAESTRO_DISCOVERY_INTERVAL_DAYS`). `MAESTRO_DISCOVERY=0`
turns the whole thing off.

Stdlib-only (plus the vault's own yaml via repos._frontmatter) and best-effort:
paths are module globals so tests monkeypatch them onto a tempdir — the same
idiom followups.py uses, and necessary because the real paths are live state.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from .. import paths
from . import repos
from .atomic_io import atomic_write_text
from .followups import is_empty_marker, section_body_any

# Vault-DATA root (Worker/_state, Worker/planner_state.json, Todo/items,
# Memory/episodes). Resolved through paths.vault_root so it follows MACP_VAULT_ROOT
# — the metrics._ROOT shape — instead of the env-immune engine-root alias that
# stages still exposes; byte-identical to the old value when the env is unset.
# start= is paths.vault_search_start(): in a checkout the engine root (the former
# parents[2]), so a cwd-upward .macp never captures it; None in an installed
# wheel, where the cwd-upward search replaces a site-packages anchor.
_ROOT = paths.vault_root(start=paths.vault_search_start())
STATE_PATH = _ROOT / "Worker" / "_state" / "discovery_state.json"
REJECTIONS_PATH = _ROOT / "Worker" / "_state" / "discovery_rejected.jsonl"
PLANNER_STATE_PATH = _ROOT / "Worker" / "planner_state.json"
ITEMS_DIR = _ROOT / "Todo" / "items"
EPISODES_DIR = _ROOT / "Memory" / "episodes"

DEFAULT_INTERVAL_DAYS = 14
# A report the planner has seen this many times without creating or rejecting
# anything is stale by the planner's own judgement; stop re-injecting it.
MAX_INJECTIONS = 3
# A project is "active" while any task that names its repo is still alive. `done`
# and `cancelled` tasks are history; a repo only they mention is not explored.
ACTIVE_STATUSES = frozenset({"open", "waiting", "blocked"})
# Frontmatter field that ties a discovery task to its repo. Deliberately NOT
# `repo:` — that field makes the runner clone into ~/Worker/<name> and run the
# external-repo pipeline; discovery reads a throwaway /tmp clone instead so it
# never contends with a real run's checkout.
REPO_FIELD = "discover_repo"
REPORT_GLOB = "*-discovery-*.md"
# [0] is canonical; English aliases match case-insensitively (followups.section_body_any).
CANDIDATE_HEADINGS = ("후보 태스크", "Candidate tasks")
CANDIDATE_SECTION = CANDIDATE_HEADINGS[0]
# Resolved reports still show the planner what they already produced (open and
# done) for this many days, so a finished candidate is not re-created.
CONSUMED_WINDOW_DAYS = 14


# ---------------------------------------------------------------- state ---

def _load_state(path: Path | None = None) -> dict:
    p = Path(path) if path else STATE_PATH
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"repos": {}, "reports": {}}
    if not isinstance(d, dict):
        return {"repos": {}, "reports": {}}
    d.setdefault("repos", {})
    d.setdefault("reports", {})
    return d


def _save_state(state: dict, path: Path | None = None) -> bool:
    p = Path(path) if path else STATE_PATH
    try:
        atomic_write_text(p, json.dumps(state, ensure_ascii=False, indent=1))
        return True
    except OSError:
        return False


def _parse_ts(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        return dt.datetime.fromisoformat(str(s)).timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- repos ---

def project_name(url: str) -> str:
    """Slug-safe short name for a repo url: `.../DripCoffee.git` -> `dripcoffee`."""
    base = repos.repo_name(url) if url else ""
    base = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return base or "project"


def active_repos(items_dir: Path | None = None) -> dict[str, dict]:
    """{repo_url: {"name", "tasks": [slugs], "open_discovery": slug|None}} for
    every repo an alive task names. A discovery task in flight for the repo is
    reported so the caller never stacks a second one."""
    d = Path(items_dir) if items_dir else ITEMS_DIR
    out: dict[str, dict] = {}
    try:
        paths = sorted(d.glob("*.md"))
    except OSError:
        return out
    for p in paths:
        fm = repos._frontmatter(p)
        if not fm:
            continue
        status = str(fm.get("status") or "").strip().lower()
        if status not in ACTIVE_STATUSES:
            continue
        mode = str(fm.get("worker_mode") or "").strip().lower()
        if mode == "discover":
            url = repos._clean(fm.get(REPO_FIELD))
            if url:
                out.setdefault(url, {"name": project_name(url), "tasks": [],
                                     "open_discovery": None})
                out[url]["open_discovery"] = p.stem
            continue
        url = repos._clean(fm.get("repo"))
        if not url:
            continue
        out.setdefault(url, {"name": project_name(url), "tasks": [],
                             "open_discovery": None})
        out[url]["tasks"].append(p.stem)
    # A repo only a discovery task names has no live work of its own: drop it,
    # or a finished project would keep being re-explored by its own discovery.
    return {u: r for u, r in out.items() if r["tasks"]}


def pick(repo_map: dict[str, dict], state: dict, *, now: float,
         interval_days: float = DEFAULT_INTERVAL_DAYS) -> str | None:
    """The one repo to explore now, or None.

    None while ANY discovery task is open (one at a time, by design), and for a
    repo explored less than `interval_days` ago. Never-explored repos go first;
    then the one whose last discovery is oldest."""
    if any(r.get("open_discovery") for r in repo_map.values()):
        return None
    due: list[tuple[float, str]] = []
    for url in sorted(repo_map):
        last = _parse_ts((state.get("repos", {}).get(url) or {}).get("last_ts"))
        if last and now - last < interval_days * 86400:
            continue
        due.append((last, url))
    if not due:
        return None
    due.sort()
    return due[0][1]


# ------------------------------------------------------------ the task ---

def task_slug(url: str, today: dt.date) -> str:
    return f"{project_name(url)}-discovery-{today.strftime('%Y%m%d')}"


def render_task(url: str, today: dt.date, tasks: list[str] | None = None) -> str:
    """The discovery task markdown. Every field the validator checks is spelled
    the way it wants (quoted allowed_tools-free, known worker_mode, list
    acceptance_criteria, no `worker_branch` placeholder)."""
    name = repos.repo_name(url)
    related = ", ".join(f"[[{t}]]" for t in (tasks or [])[:6]) or "-"
    d = today.isoformat()
    return f"""---
tags:
  - todo
  - private
  - worker
  - discovery
status: open
due: "-"
updated: {d}
priority: 35
worker_mode: discover
auto_merge: false
model_tier: standard
{REPO_FIELD}: "{url}"
outcome: "{name} 리포를 실제로 실행·전수 스윕한 결과가 Memory/episodes/{d}-*-discovery-{project_name(url)}.md 에 후보 태스크 목록(근거 포함)으로 기록된다"
next_action: "리포를 /tmp/macp-discovery/{name} 에 클론하고 README·매니페스트·빌드 스크립트에서 실행/테스트 진입점을 찾는다"
if_then: "테스트·린트·빌드·스모크 실행이 끝나면 → 전체 트리를 훑어 결함·공백·표류를 근거(file:line 또는 실행 출력)와 함께 후보 태스크로 정리한다"
estimate_p50: 45m
estimate_p90: 75m
review_cycle: "-"
acceptance_criteria:
  - "리포가 /tmp/macp-discovery/{name} 에 클론되고 main HEAD sha가 리포트에 기록된다"
  - "리포 자체의 테스트·린트·빌드 중 실행 가능한 것은 실제로 돌리고 pass/fail을 출력 근거와 함께 기록한다"
  - "헤드리스로 가능한 스모크 실행(서버 기동+응답 확인, CLI --help/샘플 실행, flutter test 등)을 timeout 안에서 1회 이상 시도하고 결과를 기록한다"
  - "전체 소스 트리를 훑어(단순 grep이 아니라 모듈별 역할 파악) 후보 태스크를 근거·예상 규모·권장 worker_mode와 함께 최대 12개 나열한다"
  - "Memory/episodes/ 에 discovery 리포트 1개, Worker/runs/<run-id>/discover.md 에 overall_status가 기록된다"
  - "대상 리포와 볼트의 tracked 파일은 하나도 수정하지 않는다 (push·commit·PR 없음)"
---

# 프로젝트 탐색 — {name} ({d})

> 플래너가 `exhausted`(만들 일이 없음)를 판정해 discovery 케이던스로 자동 생성된 태스크.
> 워커는 `discover` 단일 스테이지(`.claude/commands/worker-discover.md`)로 실행한다.

## 작업 명세 (Spec)

- **목표**: {name} 을(를) 문서가 아니라 **실행과 코드 전수 읽기**로 다시 보고, 플래너가 만들 수 있는 구체적 후보 태스크를 근거와 함께 뽑는다.
- **대상 리포**: `{url}` (읽기 전용 — /tmp 클론에서만 작업)
- **관련 열린 태스크**: {related}
- **범위**:
  - 포함: 클론, 리포 자체의 테스트/린트/빌드 실행, 헤드리스 스모크 실행, 전체 트리 스윕, 리포트 작성
  - 제외: 대상 리포 수정·커밋·push, 볼트 tracked 파일 수정, 에뮬레이터/실기기, 자격증명·네트워크 부수효과가 있는 실행, 배포/발행

## 완료 조건 (Acceptance)

- frontmatter `acceptance_criteria` 참조. 산출물은 `Memory/episodes/*-discovery-{project_name(url)}.md` 하나와 run 아티팩트 `discover.md` 하나뿐이다.

## Progress Log

- {d}: planner exhausted → discovery 케이던스로 자동 생성 (todo_system.orchestrator.discovery)
"""


def create_task(url: str, repo_map: dict[str, dict] | None = None, *,
                today: dt.date | None = None, items_dir: Path | None = None,
                state: dict | None = None, state_path: Path | None = None,
                now: float | None = None) -> Path | None:
    """Write the discovery task for `url` and stamp the repo's last_ts. None if
    the file already exists (same repo, same day) or cannot be written."""
    today = today or dt.date.today()
    d = Path(items_dir) if items_dir else ITEMS_DIR
    slug = task_slug(url, today)
    dest = d / f"{slug}.md"
    if dest.exists():
        return None
    tasks = ((repo_map or {}).get(url) or {}).get("tasks") or []
    try:
        d.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_task(url, today, tasks), encoding="utf-8")
    except OSError:
        return None
    st = state if state is not None else _load_state(state_path)
    stamp = dt.datetime.fromtimestamp(now).isoformat(timespec="seconds") \
        if now is not None else dt.datetime.now().isoformat(timespec="seconds")
    st.setdefault("repos", {})[url] = {"last_ts": stamp, "last_task": slug}
    _save_state(st, state_path)
    return dest


def planner_verdict(path: Path | None = None) -> str:
    p = Path(path) if path else PLANNER_STATE_PATH
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("verdict") or "").lower()
    except (OSError, ValueError, AttributeError):
        return ""


def enabled(env: dict | None = None) -> bool:
    e = os.environ if env is None else env
    return str(e.get("MAESTRO_DISCOVERY", "1")).strip().lower() not in ("0", "false", "no", "off")


def interval_days(env: dict | None = None) -> float:
    e = os.environ if env is None else env
    try:
        v = float(e.get("MAESTRO_DISCOVERY_INTERVAL_DAYS", DEFAULT_INTERVAL_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_DAYS
    return v if v > 0 else DEFAULT_INTERVAL_DAYS


def candidate(*, force: bool = False, now: float | None = None,
              env: dict | None = None, items_dir: Path | None = None,
              state_path: Path | None = None,
              planner_state_path: Path | None = None) -> tuple[str | None, str]:
    """The repo a discovery task WOULD be created for right now, or None and why.

    Read-only: every gate of `maybe_create` without the write, so a caller can
    ask "could discovery still find work?" (the idle-starvation detector in
    worker.py does) without spending a discovery run to learn the answer.
    Returns (repo-url-or-None, reason).
    """
    if not enabled(env):
        return None, "disabled (MAESTRO_DISCOVERY=0)"
    verdict = planner_verdict(planner_state_path)
    if not force and verdict != "exhausted":
        return None, f"planner verdict is {verdict or 'unknown'!r}, not exhausted"
    now = dt.datetime.now().timestamp() if now is None else now
    repo_map = active_repos(items_dir)
    if not repo_map:
        return None, "no active project repos"
    state = _load_state(state_path)
    url = pick(repo_map, state, now=now, interval_days=interval_days(env))
    if url is None:
        opened = [r["open_discovery"] for r in repo_map.values() if r.get("open_discovery")]
        if opened:
            return None, f"discovery already open: {opened[0]}"
        return None, "no repo due (all explored within the interval)"
    return url, f"due: {url}"


def maybe_create(*, force: bool = False, now: float | None = None,
                 env: dict | None = None, items_dir: Path | None = None,
                 state_path: Path | None = None,
                 planner_state_path: Path | None = None) -> tuple[Path | None, str]:
    """Create at most one discovery task if the planner is exhausted and a repo
    is due. Returns (path-or-None, reason)."""
    now = dt.datetime.now().timestamp() if now is None else now
    url, reason = candidate(force=force, now=now, env=env, items_dir=items_dir,
                            state_path=state_path,
                            planner_state_path=planner_state_path)
    if url is None:
        return None, reason
    repo_map = active_repos(items_dir)
    state = _load_state(state_path)
    today = dt.date.fromtimestamp(now)
    path = create_task(url, repo_map, today=today, items_dir=items_dir,
                       state=state, state_path=state_path, now=now)
    if path is None:
        return None, f"could not write task for {url}"
    return path, f"created {path.stem} for {url}"


# ----------------------------------------------------------- reports ---

def parse_candidates(text: str) -> list[str]:
    """Top-level bullets under `## 후보 태스크` (or `## Candidate tasks`) — the
    report's candidate list. `없음`/`none` bullets are skipped."""
    body = section_body_any(text, CANDIDATE_HEADINGS)
    if not body:
        return []
    out = []
    for line in body.splitlines():
        m = re.match(r"[-*][ \t]+(.+?)[ \t]*$", line)
        if m and m.group(1).strip() and not is_empty_marker(m.group(1).strip()):
            out.append(m.group(1).strip())
    return out


def _fm_field(text: str, key: str) -> str:
    fm = text[:text.find("\n---", 3)] if text.startswith("---") else ""
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.+)$", fm, re.M)
    return m.group(1).strip().strip("\"'") if m else ""


def reports(episodes_dir: Path | None = None) -> list[Path]:
    d = Path(episodes_dir) if episodes_dir else EPISODES_DIR
    try:
        return sorted(d.glob(REPORT_GLOB))
    except OSError:
        return []


def open_reports(state: dict, episodes_dir: Path | None = None) -> list[Path]:
    """Reports the planner has not resolved (created/rejected/expired)."""
    seen = state.get("reports", {})
    return [p for p in reports(episodes_dir)
            if not (seen.get(p.stem) or {}).get("resolved")]


def render_context(paths: list[Path]) -> str:
    """Planner-context block for unconsumed discovery reports, "" when none —
    empty in, empty out, so planner_tick's injection stays a no-op on the
    common path (same contract as followups.render_context)."""
    if not paths:
        return ""
    lines = [
        "## 미소비 discovery 리포트 (프로젝트를 실행·전수 스윕해 찾은 후보)",
        "",
        "아래 리포트는 discover 워커가 프로젝트를 실제로 실행하고 전체 코드를 훑어",
        "적은 후보 태스크 목록이다. 각 리포트를 Read 하고, 가드레일에 맞는 후보를",
        "골라 태스크로 만들 때 그 태스크 frontmatter에 `from_discovery: <리포트 stem>`을",
        "넣어라(그러면 reconcile이 이 목록에서 제거한다). 리포트 전체가 부적합하면",
        "`Worker/_state/discovery_rejected.jsonl`에 `{\"id\":\"<stem>\",\"reason\":\"...\"}`를",
        "append 하라. 한 리포트에서 여러 태스크를 만들어도 된다(max_new_tasks 안에서).",
        f"아무것도 안 하면 최대 {MAX_INJECTIONS}회까지 다시 보인 뒤 만료된다.",
        "",
    ]
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        repo = _fm_field(text, "repo") or "?"
        cands = parse_candidates(text)
        lines.append(f"- [{p.stem}] repo={repo} · 후보 {len(cands)}개 · "
                     f"`{p.as_posix() if not p.is_absolute() else 'Memory/episodes/' + p.name}`")
        for c in cands[:8]:
            lines.append(f"  - {c}")
    return "\n".join(lines)


_FROM_DISCOVERY_RE = re.compile(r"^from_discovery:[ \t]*(.+)$", re.M)


def tasks_by_discovery(items_dir: Path | None = None) -> dict[str, list[dict[str, str]]]:
    """{report stem: [{"slug", "status", "title"}, ...]} for EVERY task stamped
    `from_discovery: <stem>` — open and done alike, in sorted path order. The
    title is the first `# ` line of the body, the slug when there is none."""
    out: dict[str, list[dict[str, str]]] = {}
    try:
        paths = sorted(Path(items_dir if items_dir else ITEMS_DIR).glob("*.md"))
    except OSError:
        return out
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        end = text.find("\n---", 3) if text.startswith("---") else -1
        fm = text[:end] if text.startswith("---") else ""
        m = _FROM_DISCOVERY_RE.search(fm)
        if not m:
            continue
        stem = m.group(1).strip().strip("\"'")
        if not stem or stem.lower() in ("null", "none", "-"):
            continue
        body = text[end + 4:] if end >= 0 else text
        t = re.search(r"^#[ \t]+(.+?)[ \t]*$", body, re.M)
        out.setdefault(stem, []).append({
            "slug": p.stem,
            "status": _fm_field(text, "status") or "?",
            "title": t.group(1) if t else p.stem,
        })
    return out


def _tasks_from_discovery(items_dir: Path) -> dict[str, str]:
    """{report stem: first created slug} — the shape reconcile records."""
    return {stem: ts[0]["slug"] for stem, ts in tasks_by_discovery(items_dir).items()}


def _report_date(p: Path) -> dt.date | None:
    """The report's date: the stem's `YYYY-MM-DD` prefix, else frontmatter
    `generated:`, else None (the caller treats None as outside the window)."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", p.stem)
    if m:
        try:
            return dt.date.fromisoformat(m.group(1))
        except ValueError:
            pass
    try:
        gen = _fm_field(p.read_text(encoding="utf-8", errors="replace"), "generated")
        return dt.datetime.fromisoformat(gen).date() if gen else None
    except (OSError, ValueError):
        return None


def render_consumed(paths: list[Path], items_dir: Path | None = None, *,
                    now: float | None = None,
                    window_days: int = CONSUMED_WINDOW_DAYS) -> str:
    """Planner-context block: for each report of the last `window_days` days,
    every task already created from it (open and done, resolved or not). ""
    when no recent report produced a task — empty in, empty out, like
    render_context."""
    today = dt.date.fromtimestamp(now) if now is not None else dt.date.today()
    cutoff = today - dt.timedelta(days=window_days)
    created = tasks_by_discovery(items_dir)
    entries = []
    for p in paths:
        if not created.get(p.stem):
            continue
        d = _report_date(p)
        if d is not None and d >= cutoff:
            entries.append(p)
    if not entries:
        return ""
    lines = [
        f"## discovery 리포트별 이미 생성된 태스크 (최근 {window_days}일, open·done 전부)",
        "",
        "아래는 최근 discovery 리포트마다 그 리포트에서 이미 만들어진 태스크(frontmatter",
        "`from_discovery: <리포트 stem>`)를 resolved 여부와 무관하게 전부 나열한 것이다.",
        "**같은 리포트의 같은 후보가 이미 이 목록에 있으면 — open 이든 done 이든 — 다시",
        "만들지 마라.** 리포트의 후보 번호는 생성 태스크의 제목과 그 태스크 파일의 근거",
        "(outcome·Spec)를 Read 해 대조한다. slug 가 다르다고 새 후보가 아니다. 이미 done 인",
        "후보를 다시 만들면 워커가 0커밋 pass 로 끝나 사람이 중복으로 닫아야 한다.",
        "",
    ]
    for p in entries:
        try:
            repo = _fm_field(p.read_text(encoding="utf-8", errors="replace"), "repo") or "?"
        except OSError:
            repo = "?"
        ts = created[p.stem]
        lines.append(f"- [{p.stem}] repo={repo} · 생성 {len(ts)}개 · "
                     f"`{p.as_posix() if not p.is_absolute() else 'Memory/episodes/' + p.name}`")
        for t in ts:
            lines.append(f"  - {t['slug']} ({t['status']}) {t['title']}")
    return "\n".join(lines)


def _rejections(path: Path | None = None) -> dict[str, str]:
    p = Path(path) if path else REJECTIONS_PATH
    out: dict[str, str] = {}
    try:
        raw = p.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out[str(rec["id"])] = str(rec.get("reason", "")).strip()
    return out


def reconcile(*, injected: list[str] | None = None, items_dir: Path | None = None,
              episodes_dir: Path | None = None, state_path: Path | None = None,
              rejections_path: Path | None = None) -> dict[str, str]:
    """Close reports the planner acted on; count an injection for the rest.

    Returns {stem: resolution}. `injected` names the reports shown to the
    planner this tick (default: every currently-open report) — only those get
    their injection counter bumped, and one that reaches MAX_INJECTIONS without a
    task or a rejection is expired so it stops costing context."""
    state = _load_state(state_path)
    opened = open_reports(state, episodes_dir)
    if not opened:
        return {}
    created = _tasks_from_discovery(Path(items_dir) if items_dir else ITEMS_DIR)
    rejected = _rejections(rejections_path)
    shown = set(injected) if injected is not None else {p.stem for p in opened}
    out: dict[str, str] = {}
    for p in opened:
        rec = state["reports"].setdefault(p.stem, {"injections": 0, "resolved": None})
        if p.stem in created:
            rec["resolved"] = f"created: {created[p.stem]}"
        elif p.stem in rejected:
            rec["resolved"] = f"rejected: {rejected[p.stem]}" if rejected[p.stem] else "rejected"
        elif p.stem in shown:
            rec["injections"] = int(rec.get("injections", 0)) + 1
            if rec["injections"] >= MAX_INJECTIONS:
                rec["resolved"] = f"expired: unhandled after {MAX_INJECTIONS} injections"
        if rec.get("resolved"):
            out[p.stem] = rec["resolved"]
    _save_state(state, state_path)
    return out


# ---------------------------------------------------------------- cli ---

def main(argv: list[str] | None = None) -> int:
    """planner_tick.sh entry points.

    maybe-create [--force]  one discovery task when the planner is exhausted
                            (prints `created <slug>` or `skip: <reason>`)
    emit-context            unconsumed reports as a planner-context block
    emit-consumed           per-report tasks already created (open+done), last 14 days
    reconcile              close handled reports / count injections
    status                  what would happen, for a human
    """
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd == "maybe-create":
        path, reason = maybe_create(force="--force" in args)
        print(f"created {path.stem}" if path else f"skip: {reason}")
        return 0
    if cmd == "emit-context":
        sys.stdout.write(render_context(open_reports(_load_state())))
        return 0
    if cmd == "emit-consumed":
        sys.stdout.write(render_consumed(reports()))
        return 0
    if cmd == "reconcile":
        done = reconcile()
        if done:
            sys.stderr.write("discovery reconciled: " + "; ".join(
                f"{k} -> {v}" for k, v in done.items()) + "\n")
        return 0
    if cmd == "status":
        now = dt.datetime.now().timestamp()
        state = _load_state()
        repo_map = active_repos()
        for url in sorted(repo_map):
            last = (state.get("repos", {}).get(url) or {}).get("last_ts") or "never"
            od = repo_map[url].get("open_discovery") or "-"
            print(f"{repo_map[url]['name']:<24} last={last:<20} open={od} tasks={len(repo_map[url]['tasks'])}")
        nxt = pick(repo_map, state, now=now, interval_days=interval_days())
        print(f"planner verdict: {planner_verdict() or '?'} · next: {nxt or '-'} · "
              f"open reports: {len(open_reports(state))} · enabled={enabled()}")
        return 0
    sys.stderr.write("usage: python -m todo_system.orchestrator.discovery "
                     "{maybe-create [--force]|emit-context|emit-consumed|reconcile|status}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
