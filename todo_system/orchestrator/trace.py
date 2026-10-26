"""Per-task truth view — what is the CURRENT state of one task, in one screen.

`status` lists every assignment; nothing answered "what is going on with THIS
slug". Diagnosing ai-stock-trading-23-accuracy-alpha-grid (2026-08-17) meant
opening five run dirs by hand and still getting it wrong, because a run dir no
longer means one execution: inherit_artifacts() copies a prior run's artifacts
in, and the verify.md sitting in the oldest dir was read as if it described the
newest code.

So this view answers the four questions that misdiagnosis needed:
  1. which run dir is the latest, and which stages does it claim
  2. per artifact — this run's own work, or a copy, and from where
  3. what the ledger currently believes (state / refail / blocker)
  4. does the branch exist, and did its PR merge

STRICTLY READ-ONLY, by construction rather than by promise: there is no write()
sibling to dashboard.py's, nothing here opens a file for writing or shells out,
and render() is a pure function of (filesystem, injected lookups) — no clock, so
its output is reproducible. A diagnostic with side effects causes incidents
during incident response. Stdlib-only.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import paths
from . import assignment, stages
from .dashboard import _merged_park     # the predicate only, never its write()

# Artifacts in pipeline order, so the view reads top-to-bottom like the run did.
_STAGE_ORDER = stages.STAGES_AUTO + stages.STAGES_DELEGATE + stages.STAGES_QUICK

_RUN_ID_RE = re.compile(r"^run_id:[ \t]*(\S+)[ \t]*$", re.M)


def runs_for(slug: str, runs_dir: Path) -> list[Path]:
    """Re-export of stages.runs_for — the `<timestamp>-<slug>` match is defined
    there and nowhere else, so this view and the retry-inheritance path can no
    longer drift apart. Oldest first."""
    return stages.runs_for(slug, runs_dir)


def _author_run_id(path: Path) -> str | None:
    """The run_id the artifact's own frontmatter claims — the run that WROTE it.

    Weaker evidence than an `inherited_from:` stamp (an agent can write the wrong
    id, and it is absent from run.log entirely), so it is only ever used to
    explain an unstamped artifact, never to assert one was inherited."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    m = _RUN_ID_RE.search(text[:end] if end != -1 else text[:400])
    return m.group(1) if m else None


def artifact_origin(run_dir: Path, artifact: Path) -> tuple[str, str | None]:
    """(kind, detail) for one artifact — 'own' | 'inherited' | 'unknown' | 'missing'.

    Answered from the ARTIFACT, never from stages.json: the runner calls
    record_stage() for inherited stages too, so stages.json lists a copied stage
    exactly like one this run executed."""
    if not artifact.exists():
        return ("missing", None)
    src = stages.read_provenance(artifact)
    if src:
        return ("inherited", src)
    author = _author_run_id(artifact)
    if author == Path(run_dir).name:
        return ("own", None)
    return ("unknown", author)


_ORIGIN_TEXT = {
    "own": "이번 런",
    "missing": "없음",
}


def _origin_line(stage: str, name: str, kind: str, detail: str | None) -> str:
    if kind == "inherited":
        return f"- `{name}` ({stage}) — **상속본** ← `{detail}`"
    if kind == "unknown" and detail:
        # a real red flag: the file says another run wrote it, yet carries no
        # inheritance stamp — a pre-stamp copy, or an agent that wrote a wrong id
        return (f"- `{name}` ({stage}) — ⚠ **출처 불명**: 파일의 run_id는 `{detail}` "
                "(스탬프 도입 이전의 상속본이거나 잘못 쓰인 run_id)")
    if kind == "unknown":
        # nothing claims an author (run.log has no frontmatter at all; some
        # artifacts have one without run_id). A successful inheritance always
        # leaves a stamp, so its absence points at 'own' — but a pre-stamp run or
        # a failed stamp looks identical from here, hence presumption rather than certainty.
        return (f"- `{name}` ({stage}) — 출처 표시 없음: **이번 런 생성으로 추정** "
                "(상속이면 스탬프가 남으므로 — 단 스탬프 도입 이전 런이면 확증 불가)")
    return f"- `{name}` ({stage}) — {_ORIGIN_TEXT[kind]}"


def _latest_run_section(slug: str, runs: list[Path]) -> list[str]:
    if not runs:
        return ["## 최신 런", "", f"- 런 없음 (`*-{slug}` 디렉터리가 하나도 없음)"]
    latest = runs[-1]
    done = stages.completed_stages(latest)
    out = ["## 최신 런", "",
           f"- run_dir: `{latest}`",
           f"- 런 {len(runs)}개 중 마지막 ({len(runs)}/{len(runs)})",
           f"- stages.json completed: {', '.join(done) if done else '(없음)'}",
           "",
           "### 산출물 출처 (판정 근거는 stages.json이 아니라 파일 자체의 스탬프)", ""]
    for stage in _STAGE_ORDER:
        name = stages.STAGE_ARTIFACT.get(stage)
        art = latest / name
        kind, detail = artifact_origin(latest, art)
        if kind == "missing" and stage not in done:
            continue          # never ran, never inherited — nothing to say
        out.append(_origin_line(stage, name, kind, detail))
    return out


def _verify_section(runs: list[Path]) -> list[str]:
    """Every verify verdict this task ever produced, each with how stale it is.

    verify is NOT in RESUMABLE, so a failed run's verify.md stays behind in ITS
    OWN run dir while later runs inherit the research/plan/implement around it.
    Reading such a verdict as current is the exact misdiagnosis this view exists
    to prevent, so the age is printed next to every verdict, not in a footnote."""
    out = ["## verify 판정 (⚠ verify는 상속되지 않음 — 판정은 그 런의 디렉터리에만 남는다)", ""]
    seen = False
    for i, rd in enumerate(runs, start=1):
        vp = rd / stages.STAGE_ARTIFACT["verify"]
        if not vp.exists():
            continue
        seen = True
        text = vp.read_text(encoding="utf-8", errors="replace")
        verdict = stages.parse_verify_verdict(text) or "판정 없음"
        blocked = stages.parse_blocked_on(text)
        after = len(runs) - i
        stale = (f" — ⚠ 이 판정은 이후 {after}번의 런 **이전** 상태"
                 if after else " — 최신 런의 판정")
        out.append(f"- 런 {i}/{len(runs)} `{rd.name}`: **{verdict}**{stale}")
        if blocked:
            out.append(f"  - blocked_on: {blocked}")
    if not seen:
        out.append(f"- verify.md 를 남긴 런이 하나도 없음 (런 {len(runs)}개 전부) "
                   "— 이 태스크는 아직 검증된 적이 없다")
    return out


def _ledger_section(slug: str, ledger) -> list[str]:
    out = ["## 원장 (assignment ledger)", ""]
    if ledger is None:
        return out + ["- 원장 조회 안 함 (ledger 미주입)"]
    a = ledger.load(slug)
    if a is None:
        return out + [f"- `{slug}` 에 대한 assignment 없음 (미할당이거나 이미 정리됨)"]
    shown = a.display_state
    stored = f" (원장 저장값: {a.state} — 실패 아님, 자동 재시도)" if shown != a.state else ""
    out += [f"- state: **{shown}**{stored} (agent: `{a.agent_id}`)",
            f"- attempts: {a.attempts} · refail_count: {a.refail_count} "
            f"· capacity_retry_count: {a.capacity_retry_count}"]
    if a.state == assignment.AWAITING_HUMAN:
        # The single most common "why is this not running?" — and the one the
        # rest of this trace cannot answer. load_dispatchable can list the task
        # every tick and dispatch() will still return None immediately, because a
        # non-terminal ledger row short-circuits it before routing. Say so right
        # under `state`, before the reader goes hunting through todo.db and CI.
        out.append("- ⛔ 원장이 **파킹** 상태 — todo.db/`load_dispatchable`가 이 태스크를 "
                   "후보로 올려도 `dispatch()`가 라우팅 전에 즉시 None을 반환한다")
        if a.blocked_on and _merged_park(a):
            # reconcile_merged parked this row: its PR merged, a step a human owes
            # is still open. The recipe below is wrong here — reconcile_unparked
            # would flip the row to FAILED and clear the blocker, sending merged
            # work back down the retry path. The check is dashboard's own function,
            # so this view and Orchestrator.md cannot name different exits.
            out.append("  - 종결: PR은 이미 머지됨 — 남은 사람 단계를 마친 뒤 태스크 md를 "
                       "`status: done`으로 닫으면 다음 틱에 `reconcile_merged()`가 "
                       "\"todo already closed\" 경로로 자동 종결한다 — 재큐 불필요 "
                       "(원장 수기 편집도 불필요)")
        elif a.blocked_on:
            out.append("  - 해제: 블로커 해소 후 태스크 md의 `worker_mode`를 비-디스패치 "
                       "값(`blocked` 등)으로 바꿨다가 `auto`로 되돌리면 "
                       "`reconcile_unparked()`가 그 전이를 해제 신호로 읽어 자동 재큐한다 "
                       "— 원장 수기 편집 불필요")
    if a.blocked_on:
        out.append(f"- blocked_on: {a.blocked_on}")
    if a.job_running:
        out.append("- long_job 실행 중")
    if getattr(a, "checkout_busy", False):
        out.append("- 체크아웃 대기 (다른 런이 같은 체크아웃 사용 중 — 끝나면 자동 재시도)")
    if a.last_note:
        out.append(f"- last_note: {a.last_note}")
    rd = a.payload.get("run_dir")
    if rd:
        out.append(f"- 원장이 가리키는 run_dir: `{rd}`")
    return out


def _branch_section(slug: str, repo_dir: Path, branch_lookup, pr_lookup) -> list[str]:
    branch = f"worker/{slug}"
    out = ["## 브랜치 · PR", "", f"- branch: `{branch}`"]
    try:
        pushed = branch_lookup(repo_dir, branch)
    except Exception:
        pushed = None       # no git / no network — unknown, not "no branch"
    if pushed is None:
        out.append("- origin에 존재: unknown (git 조회 실패 — 실패가 아님)")
    else:
        out.append("- origin에 존재: "
                   + ("예" if pushed else "아니오 (미push, 또는 머지 후 삭제됨)"))
    try:
        merged = pr_lookup(branch)
    except Exception:
        merged = None
    # None is a NORMAL answer (no gh, not authenticated, no PR for the branch) —
    # rendering it as a failure would send a human hunting for a break that is
    # just a missing CLI. Same contract gh_pr_merged() documents.
    out.append("- PR: " + {True: "머지됨",
                           False: "존재하지만 아직 머지 안 됨 (open/closed)"}
               .get(merged, "unknown (gh 없음/미인증/PR 없음 — 실패가 아님)"))
    return out


def render(slug: str, ledger=None, *, runs_dir: Path | None = None,
           repo_dir: Path | None = None, pr_lookup=None, branch_lookup=None) -> str:
    """The whole view for one slug, as markdown. Writes nothing.

    Paths and lookups resolve per CALL, not at import: stages.RUNS_DIR is
    monkeypatched by tests and `runner --daemon` is long-lived, so an
    import-time default would freeze the wrong value (the same trap stage_model()
    documents in stages.py)."""
    runs_dir = Path(runs_dir) if runs_dir is not None else stages.RUNS_DIR
    repo_dir = (Path(repo_dir) if repo_dir is not None
                else paths.vault_root(start=paths.vault_search_start()))
    pr_lookup = pr_lookup or stages.gh_pr_merged
    branch_lookup = branch_lookup or stages.remote_branch_exists

    runs = runs_for(slug, runs_dir)
    out = [f"# 🔎 trace — `{slug}`", "",
           f"> 런 {len(runs)}개 · runs_dir `{runs_dir}` · 읽기 전용 (상태를 바꾸지 않음)", ""]
    out += _latest_run_section(slug, runs) + [""]
    out += _verify_section(runs) + [""]
    out += _ledger_section(slug, ledger) + [""]
    out += _branch_section(slug, repo_dir, branch_lookup, pr_lookup) + [""]
    return "\n".join(out)
