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

Human text comes from i18n.t() at call time, so the language follows
paths.ui_language() (en by default, ko for a ko vault) even in a long-lived
daemon; only data values (slugs, blocked_on, last_note) pass through untranslated.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import i18n, paths
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


# Keys, not text: the translation is looked up per call (see the docstring).
_ORIGIN_KEY = {
    "own": "trace.origin.own",
    "missing": "trace.origin.missing",
}


def _origin_line(stage: str, name: str, kind: str, detail: str | None) -> str:
    if kind == "inherited":
        return i18n.t("trace.origin.inherited", name=name, stage=stage, detail=detail)
    if kind == "unknown" and detail:
        # a real red flag: the file says another run wrote it, yet carries no
        # inheritance stamp — a pre-stamp copy, or an agent that wrote a wrong id
        return i18n.t("trace.origin.foreign_author", name=name, stage=stage, detail=detail)
    if kind == "unknown":
        # nothing claims an author (run.log has no frontmatter at all; some
        # artifacts have one without run_id). A successful inheritance always
        # leaves a stamp, so its absence points at 'own' — but a pre-stamp run or
        # a failed stamp looks identical from here, hence presumption rather than certainty.
        return i18n.t("trace.origin.presumed_own", name=name, stage=stage)
    return f"- `{name}` ({stage}) — {i18n.t(_ORIGIN_KEY[kind])}"


def _latest_run_section(slug: str, runs: list[Path]) -> list[str]:
    if not runs:
        return [i18n.t("trace.latest.heading"), "",
                i18n.t("trace.latest.no_runs", slug=slug)]
    latest = runs[-1]
    done = stages.completed_stages(latest)
    completed = ", ".join(done) if done else i18n.t("trace.latest.none")
    out = [i18n.t("trace.latest.heading"), "",
           f"- run_dir: `{latest}`",
           i18n.t("trace.latest.position", n=len(runs)),
           f"- stages.json completed: {completed}",
           "",
           i18n.t("trace.latest.provenance_heading"), ""]
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
    out = [i18n.t("trace.verify.heading"), ""]
    seen = False
    for i, rd in enumerate(runs, start=1):
        vp = rd / stages.STAGE_ARTIFACT["verify"]
        if not vp.exists():
            continue
        seen = True
        text = vp.read_text(encoding="utf-8", errors="replace")
        verdict = stages.parse_verify_verdict(text) or i18n.t("trace.verify.no_verdict")
        blocked = stages.parse_blocked_on(text)
        after = len(runs) - i
        stale = (i18n.t("trace.verify.stale", after=after)
                 if after else i18n.t("trace.verify.latest"))
        out.append(i18n.t("trace.verify.line", i=i, n=len(runs), run=rd.name,
                          verdict=verdict, stale=stale))
        if blocked:
            out.append(f"  - blocked_on: {blocked}")
    if not seen:
        out.append(i18n.t("trace.verify.none", n=len(runs)))
    return out


def _ledger_section(slug: str, ledger) -> list[str]:
    out = [i18n.t("trace.ledger.heading"), ""]
    if ledger is None:
        return out + [i18n.t("trace.ledger.not_injected")]
    a = ledger.load(slug)
    if a is None:
        return out + [i18n.t("trace.ledger.no_assignment", slug=slug)]
    shown = a.display_state
    stored = i18n.t("trace.ledger.stored", state=a.state) if shown != a.state else ""
    out += [f"- state: **{shown}**{stored} (agent: `{a.agent_id}`)",
            f"- attempts: {a.attempts} · refail_count: {a.refail_count} "
            f"· capacity_retry_count: {a.capacity_retry_count}"]
    if a.state == assignment.AWAITING_HUMAN:
        # The single most common "why is this not running?" — and the one the
        # rest of this trace cannot answer. load_dispatchable can list the task
        # every tick and dispatch() will still return None immediately, because a
        # non-terminal ledger row short-circuits it before routing. Say so right
        # under `state`, before the reader goes hunting through todo.db and CI.
        out.append(i18n.t("trace.ledger.parked"))
        if a.blocked_on and _merged_park(a):
            # reconcile_merged parked this row: its PR merged, a step a human owes
            # is still open. The recipe below is wrong here — reconcile_unparked
            # would flip the row to FAILED and clear the blocker, sending merged
            # work back down the retry path. The check is dashboard's own function,
            # so this view and Orchestrator.md cannot name different exits.
            out.append(i18n.t("trace.ledger.merged_exit"))
        elif a.blocked_on:
            out.append(i18n.t("trace.ledger.unpark"))
    if a.blocked_on:
        out.append(f"- blocked_on: {a.blocked_on}")
    if a.job_running:
        out.append(i18n.t("trace.ledger.long_job"))
    if getattr(a, "checkout_busy", False):
        out.append(i18n.t("trace.ledger.checkout_busy"))
    if a.last_note:
        out.append(f"- last_note: {a.last_note}")
    rd = a.payload.get("run_dir")
    if rd:
        out.append(i18n.t("trace.ledger.run_dir", run_dir=rd))
    return out


def _branch_section(slug: str, repo_dir: Path, branch_lookup, pr_lookup) -> list[str]:
    branch = f"worker/{slug}"
    out = [i18n.t("trace.branch.heading"), "", f"- branch: `{branch}`"]
    try:
        pushed = branch_lookup(repo_dir, branch)
    except Exception:
        pushed = None       # no git / no network — unknown, not "no branch"
    if pushed is None:
        out.append(i18n.t("trace.branch.origin_unknown"))
    else:
        out.append(i18n.t("trace.branch.origin_yes" if pushed
                          else "trace.branch.origin_no"))
    try:
        merged = pr_lookup(branch)
    except Exception:
        merged = None
    # None is a NORMAL answer (no gh, not authenticated, no PR for the branch) —
    # rendering it as a failure would send a human hunting for a break that is
    # just a missing CLI. Same contract gh_pr_merged() documents.
    out.append(i18n.t({True: "trace.branch.pr_merged",
                       False: "trace.branch.pr_open"}
                      .get(merged, "trace.branch.pr_unknown")))
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
    out = [i18n.t("trace.title", slug=slug), "",
           i18n.t("trace.summary", n=len(runs), runs_dir=runs_dir), ""]
    out += _latest_run_section(slug, runs) + [""]
    out += _verify_section(runs) + [""]
    out += _ledger_section(slug, ledger) + [""]
    out += _branch_section(slug, repo_dir, branch_lookup, pr_lookup) + [""]
    return "\n".join(out)
