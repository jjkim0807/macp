"""Autonomous-value metrics — is the orchestrator actually net-positive?

Renders a small dashboard (Todo/Metrics.md) from the assignment ledger, todo.db,
and Worker/runs so you can see throughput, execution reliability, a cost proxy,
and a rework proxy (the quality signal that matters: how often autonomous output
had to be redone). Without this you're flying blind on whether the system pays
for itself. Stdlib-only; run with `python -m todo_system.orchestrator.metrics`.
"""
from __future__ import annotations

import glob
import json
import re
import sqlite3
import statistics
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from .. import paths
from . import message as m
from . import stages
from .assignment import is_deferred

# Vault-data root. start= is paths.vault_search_start(): the engine root in a
# checkout (byte-identical to the former parents[2] anchor), None in an
# installed wheel so the cwd-upward search runs instead of a site-packages anchor.
_ROOT = paths.vault_root(start=paths.vault_search_start())
LEDGER = _ROOT / "Worker" / "assignments"
RUNS = _ROOT / "Worker" / "runs"
DB = _ROOT / "Todo" / "todo.db"
DEFAULT_OUT = _ROOT / "Todo" / "Metrics.md"


def _load_ledger() -> list[dict]:
    out = []
    for f in glob.glob(str(LEDGER / "*.json")):
        try:
            out.append(json.load(open(f, encoding="utf-8")))
        except Exception:
            pass
    return out


def _db_counts() -> dict:
    try:
        con = sqlite3.connect(str(DB))
        done = con.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
        auto_done = con.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='done' AND worker_mode IN ('auto','delegate')"
        ).fetchone()[0]
        cancelled = con.execute("SELECT COUNT(*) FROM tasks WHERE status='cancelled'").fetchone()[0]
        return {"done": done, "auto_done": auto_done, "cancelled": cancelled}
    except Exception:
        return {"done": 0, "auto_done": 0, "cancelled": 0}


def _reverts_in_vault() -> int:
    """worker/ merges later reverted in the vault (a hard rework signal)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(_ROOT), "log", "--all", "--grep=Revert", "--grep=revert",
             "-i", "--oneline", "--since=30.days"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
        return sum(1 for ln in (r.stdout or "").splitlines() if "worker/" in ln.lower() or "revert" in ln.lower())
    except Exception:
        return 0


# --- Per-stage pipeline breakdown -------------------------------------------
# Answers the standing question "does the 5-stage pipeline pay for itself?" with
# a decomposition the flat proxies above cannot: which stage costs what, and how
# often each mode reworks. The data has TWO populations, deliberately kept
# distinct: the assignment ledger (Worker/assignments/*.json) is the durable,
# full-population record of every task and its retries; Worker/runs/ is gitignored
# and ephemeral (.gitignore:60), so the run_dirs on disk are only a recent local
# SAMPLE. Routing + rework come from the ledger; per-stage calls/bytes/verdicts
# from the sample. Both Ns are reported so a reader never mistakes one for the
# other. Real token/cost figures exist only for runs since 2026-09-19: the runner
# now reads each stage's usage back from the CLI's transcript into stages.json
# `usage` (see the comment above stages.USAGE_ENV). Older run_dirs carry none, so
# artifact bytes stay the per-stage input-cost proxy across the whole sample and
# `stage_usage` reports its own, smaller N. See Notes/macp-pipeline-cost-benefit.md.

_TS_PREFIX = re.compile(r"^\d{8}T\d{6}-")


def _slug_of(run_dir_name: str) -> str:
    """Run dir is `<YYYYMMDDThhmmss>-<slug>`; strip ONLY the timestamp prefix.

    Anchored with `^` so a slug that merely contains digits is never truncated —
    the inverse of the suffix over-match that once made resumable_run glob too
    greedily ([[macp-resumable-run-glob-matches-slug-suffix]])."""
    return _TS_PREFIX.sub("", run_dir_name)


def _run_stage_calls(run_dir: Path) -> list[str]:
    """Stages a run actually executed. stages.json `completed` is the truth; a run
    predating stages.json falls back to artifact-on-disk presence."""
    sj = run_dir / "stages.json"
    if sj.exists():
        try:
            comp = json.loads(sj.read_text(encoding="utf-8")).get("completed")
            if isinstance(comp, list):
                return [str(s) for s in comp]
        except Exception:
            pass
    return [st for st, art in stages.STAGE_ARTIFACT.items() if (run_dir / art).exists()]


def _run_usage(run_dir: Path) -> dict | None:
    """stages.json `usage` of one run ({stage: record}), or None for a run that
    predates token instrumentation — or ran with it switched off."""
    try:
        u = json.loads((run_dir / "stages.json").read_text(encoding="utf-8")).get("usage")
    except Exception:
        return None
    return u if isinstance(u, dict) and u else None


def _usage_counted(rec) -> bool:
    """A record with real token counts: not `unavailable`, not damaged."""
    return (isinstance(rec, dict) and "unavailable" not in rec
            and all(isinstance(rec.get(k), int) and not isinstance(rec.get(k), bool)
                    for k in stages.USAGE_TOKEN_KEYS))


def _stage_usage(rows: dict, unavailable: Counter, runs_with_usage: int) -> dict:
    """Per stage: N counted records, how many were unavailable, Σ and median of
    the four token counters plus quota_tokens (input + cache_creation + output —
    cache reads left out), and Σ of the CLI's cost estimate over the records that
    carry one (cost_n of N)."""
    out = {}
    for st in sorted(set(rows) | set(unavailable)):
        recs = rows.get(st, [])
        series = {k: [r[k] for r in recs] for k in stages.USAGE_TOKEN_KEYS}
        series["quota_tokens"] = [r["input_tokens"] + r["cache_creation_input_tokens"]
                                  + r["output_tokens"] for r in recs]
        costs = [r["total_cost_usd"] for r in recs
                 if isinstance(r.get("total_cost_usd"), (int, float))
                 and not isinstance(r.get("total_cost_usd"), bool)]
        out[st] = {
            "n": len(recs), "unavailable": unavailable.get(st, 0),
            "sum": {k: sum(v) for k, v in series.items()},
            "median": {k: statistics.median(v) for k, v in series.items()} if recs else {},
            "cost_usd": round(sum(costs), 6) if costs else None, "cost_n": len(costs),
        }
    return {"runs_with_usage": runs_with_usage, "stages": out}


def pipeline_breakdown(runs: str | Path | None = None,
                       ledger: str | Path | None = None) -> dict:
    """Per-stage decomposition of the 5-stage pipeline's cost and reliability.

    Pure over its two path args (None → the module constants, resolved at CALL
    time like render() does, so a monkeypatch of RUNS/LEDGER is honoured) so it is
    testable against a tmp fixture. Read-only. Structure:
      - routing / routing_totals: worker_mode × state, from ledger payload
      - rework_by_mode / rework: attempts>1, refail_count, ci-fix — from ledger
      - stage_calls: per-run `completed` (fallback artifact presence), summed
      - stage_bytes: Σ artifact size per stage (re-input cost proxy)
      - verify_verdicts: parse_verify_verdict tally (PASS/FAIL/SKIP/UNSTATED)
      - redispatch: run_dir count grouped by slug → tasks that ran >1x
      - stage_usage: stages.json `usage` per stage (N / unavailable / Σ / median
        / cost) over the runs_with_usage run_dirs that carry it
    ledger_n and run_dirs_n are both returned because they are DIFFERENT
    populations (the ledger is durable; Worker/runs/ is gitignored + ephemeral)."""
    runs = Path(runs) if runs is not None else RUNS
    ledger = Path(ledger) if ledger is not None else LEDGER
    led: list[dict] = []
    for f in glob.glob(str(ledger / "*.json")):
        try:
            led.append(json.load(open(f, encoding="utf-8")))
        except Exception:
            pass
    mode_of = lambda d: (d.get("payload") or {}).get("worker_mode") or d.get("worker_mode") or "?"  # noqa: E731

    routing: dict = defaultdict(lambda: defaultdict(int))
    routing_totals: Counter = Counter()
    rework_by_mode: dict = defaultdict(
        lambda: {"n": 0, "done": 0, "retried": 0, "refailed": 0, "refail_total": 0})
    retried = refailed = refail_total = ci_fix = 0
    for d in led:
        mo = mode_of(d)
        state = d.get("state", "?")
        routing[mo][state] += 1
        routing_totals[mo] += 1
        r = rework_by_mode[mo]
        r["n"] += 1
        if state == "done":
            r["done"] += 1
        rc = int(d.get("refail_count", 0) or 0)
        if int(d.get("attempts", 1) or 1) > 1:
            retried += 1
            r["retried"] += 1
        if rc > 0:
            refailed += 1
            r["refailed"] += 1
        r["refail_total"] += rc
        refail_total += rc
        if "ci-fix" in (d.get("task_id") or ""):
            ci_fix += 1

    run_dirs = [Path(d) for d in glob.glob(str(runs / "*")) if Path(d).is_dir()]
    stage_calls: Counter = Counter()
    stage_bytes: Counter = Counter()
    verify_verdicts: Counter = Counter()
    slug_runs: Counter = Counter()
    usage_rows: dict = defaultdict(list)
    usage_unavailable: Counter = Counter()
    runs_with_usage = 0
    for d in run_dirs:
        for st in _run_stage_calls(d):
            stage_calls[st] += 1
        usage = _run_usage(d)
        if usage is not None:
            runs_with_usage += 1
            for st, rec in usage.items():
                if _usage_counted(rec):
                    usage_rows[str(st)].append(rec)
                else:
                    usage_unavailable[str(st)] += 1
        for st, art in stages.STAGE_ARTIFACT.items():
            p = d / art
            if p.exists():
                stage_bytes[st] += p.stat().st_size
        vm = d / stages.STAGE_ARTIFACT["verify"]
        if vm.exists():
            verdict = stages.parse_verify_verdict(vm.read_text(encoding="utf-8", errors="replace"))
            verify_verdicts[verdict or "UNSTATED"] += 1
        slug_runs[_slug_of(d.name)] += 1
    multi = {s: n for s, n in slug_runs.items() if n > 1}

    return {
        "ledger_n": len(led),
        "run_dirs_n": len(run_dirs),
        "routing": {mo: dict(states) for mo, states in routing.items()},
        "routing_totals": dict(routing_totals),
        "rework_by_mode": {mo: dict(v) for mo, v in rework_by_mode.items()},
        "rework": {"retried": retried, "refailed": refailed,
                   "refail_total": refail_total, "ci_fix": ci_fix},
        "stage_calls": dict(stage_calls),
        "stage_bytes": dict(stage_bytes),
        "verify_verdicts": dict(verify_verdicts),
        "redispatch": {"run_dirs": len(run_dirs), "distinct_slugs": len(slug_runs),
                       "redispatched_slugs": len(multi), "multi": multi},
        "stage_usage": _stage_usage(usage_rows, usage_unavailable, runs_with_usage),
    }


# Token bases for report_stage_share: io = input + output; quota = input +
# cache_creation + output (the _stage_usage definition); all = the four counters.
_SHARE_BASES = {
    "io": ("input_tokens", "output_tokens"),
    "quota": ("input_tokens", "cache_creation_input_tokens", "output_tokens"),
    "all": stages.USAGE_TOKEN_KEYS,
}
REPORT_SHARE_THRESHOLD = 0.10


def _cost_of(rec: dict):
    c = rec.get("total_cost_usd")
    return c if isinstance(c, (int, float)) and not isinstance(c, bool) else None


def _share_summary(report_vals: list, pipeline_vals: list) -> dict:
    ratios = [r / p for r, p in zip(report_vals, pipeline_vals) if p]
    total = sum(pipeline_vals)
    return {
        "report_median": statistics.median(report_vals) if report_vals else None,
        "report_sum": sum(report_vals),
        "pipeline_sum": total,
        "pooled_ratio": round(sum(report_vals) / total, 4) if total else None,
        "median_run_ratio": round(statistics.median(ratios), 4) if ratios else None,
    }


def report_stage_share(runs: str | Path | None = None) -> dict:
    """Report stage's share of the 5-stage pipeline's usage, over Worker/runs.

    Sample: a run counts only when stages.json carries `usage`, `completed` holds
    all of STAGES_AUTO, and every one of those five records is counted
    (_usage_counted). Otherwise it is excluded as no_usage / not_full_pipeline /
    partial_usage — a missing stage would understate the pipeline total and
    inflate the share. Cost sums only over runs whose five records all carry a
    numeric total_cost_usd (cost_n of n).

    The verdict is the pooled cost share against REPORT_SHARE_THRESHOLD; with no
    cost-bearing run it falls back to the pooled quota-token share, and says so in
    primary_basis. Pure over `runs` (None → RUNS at call time). Read-only."""
    runs = Path(runs) if runs is not None else RUNS
    run_dirs = [Path(d) for d in glob.glob(str(runs / "*")) if Path(d).is_dir()]
    order = list(stages.STAGES_AUTO)
    excluded: Counter = Counter()
    partial_by_stage: Counter = Counter()
    report_vals: dict = {b: [] for b in _SHARE_BASES}
    pipeline_vals: dict = {b: [] for b in _SHARE_BASES}
    cost_report: list = []
    cost_pipeline: list = []
    for d in sorted(run_dirs):
        usage = _run_usage(d)
        if usage is None:
            excluded["no_usage"] += 1
            continue
        if not set(order) <= set(_run_stage_calls(d)):
            excluded["not_full_pipeline"] += 1
            continue
        missing = [st for st in order if not _usage_counted(usage.get(st))]
        if missing:
            excluded["partial_usage"] += 1
            partial_by_stage.update(missing)
            continue
        for basis, keys in _SHARE_BASES.items():
            per_stage = {st: sum(usage[st][k] for k in keys) for st in order}
            report_vals[basis].append(per_stage["report"])
            pipeline_vals[basis].append(sum(per_stage.values()))
        costs = [_cost_of(usage[st]) for st in order]
        if all(c is not None for c in costs):
            cost_report.append(costs[order.index("report")])
            cost_pipeline.append(sum(costs))

    n = len(report_vals["io"])
    bases = {b: _share_summary(report_vals[b], pipeline_vals[b]) for b in _SHARE_BASES}
    cost = _share_summary(cost_report, cost_pipeline)
    for k in ("report_median", "report_sum", "pipeline_sum"):
        if cost[k] is not None:
            cost[k] = round(cost[k], 6)
    if cost_report:
        primary_basis, primary_ratio = "cost_usd", cost["pooled_ratio"]
    else:
        primary_basis, primary_ratio = "quota", bases["quota"]["pooled_ratio"]
    if n == 0 or primary_ratio is None:
        verdict = "insufficient"
    elif primary_ratio < REPORT_SHARE_THRESHOLD:
        verdict = "keep"
    else:
        verdict = "de-model"
    return {
        "run_dirs_n": len(run_dirs), "n": n, "cost_n": len(cost_report),
        "excluded": {k: excluded.get(k, 0)
                     for k in ("no_usage", "not_full_pipeline", "partial_usage")},
        "partial_by_stage": dict(partial_by_stage),
        "bases": bases, "cost": cost,
        "primary_basis": primary_basis, "primary_ratio": primary_ratio,
        "threshold": REPORT_SHARE_THRESHOLD, "verdict": verdict,
    }


VERIFY_FAIL_RETRY_MIN_N = 5


def _verify_text(run_dir: Path) -> str | None:
    try:
        return (run_dir / stages.STAGE_ARTIFACT["verify"]).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None


def _tokens_of(recs: list[dict], basis: str) -> int:
    return sum(r[k] for r in recs for k in _SHARE_BASES[basis])


def verify_fail_retry_cost(runs: str | Path | None = None) -> dict:
    """What a verify-FAIL retry spends re-buying research + plan, over Worker/runs.

    Pair: two consecutive run_dirs (F, R) of the same slug (exact `_slug_of`
    match, oldest first — the same set stages.runs_for returns, so no suffix
    over-match) where F's verify.md parses as FAIL. R is the retry; a slug with
    three FAILs in a row yields several pairs. A pair enters the sample (n) only
    when R's stages.json `usage.research` is counted and carries a cost; the rest
    are tallied by reason — no_retry (F is the slug's last run), no_usage (R has
    no `usage`), no_research_usage (research missing / unavailable / costless:
    quick or inherited retries, research timeouts). A counted plan with a cost is
    added to rp_cost; a pair without one stays in and bumps plan_missing.

    retry_total_cost is every counted, cost-bearing record of R (what the retry
    actually spent, finished or not). sample_spend is the same sum over EVERY
    run_dir with usage — the pipeline spend of the period, the lever's ceiling.

    The function deliberately stops at numbers + `sufficient`: whether a FAIL was
    a plan defect, an implementation defect or the environment is not recorded
    anywhere in verify.md (only blocked_on, surfaced as cause_hint), so a human
    classifies each pair. See Notes/macp-pipeline-cost-benefit.md. Pure over
    `runs` (None → RUNS at call time). Read-only."""
    runs = Path(runs) if runs is not None else RUNS
    run_dirs = sorted(Path(d) for d in glob.glob(str(runs / "*")) if Path(d).is_dir())
    by_slug: dict = defaultdict(list)
    sample_spend = 0.0
    for d in run_dirs:
        by_slug[_slug_of(d.name)].append(d)
        for rec in (_run_usage(d) or {}).values():
            if _usage_counted(rec) and _cost_of(rec) is not None:
                sample_spend += _cost_of(rec)

    excluded: Counter = Counter()
    excluded_pairs: list = []
    pairs: list = []
    plan_missing = 0
    for slug in sorted(by_slug):
        ds = by_slug[slug]
        for i, f in enumerate(ds):
            ftext = _verify_text(f)
            if stages.parse_verify_verdict(ftext or "") != "FAIL":
                continue
            r = ds[i + 1] if i + 1 < len(ds) else None
            usage = _run_usage(r) if r is not None else None
            research = (usage or {}).get("research")
            if r is None:
                reason = "no_retry"
            elif usage is None:
                reason = "no_usage"
            elif not _usage_counted(research) or _cost_of(research) is None:
                reason = "no_research_usage"
            else:
                reason = None
            if reason:
                excluded[reason] += 1
                excluded_pairs.append({"slug": slug, "fail_run": f.name,
                                       "retry_run": r.name if r is not None else None,
                                       "reason": reason})
                continue
            plan = usage.get("plan")
            plan_cost = _cost_of(plan) if _usage_counted(plan) else None
            if plan_cost is None:
                plan_missing += 1
            rp_recs = [research] + ([plan] if plan_cost is not None else [])
            counted = [rec for rec in usage.values() if _usage_counted(rec)]
            blocked = stages.parse_blocked_on(ftext or "")
            rtext = _verify_text(r)
            pairs.append({
                "slug": slug, "fail_run": f.name, "retry_run": r.name,
                "blocked_on": blocked, "cause_hint": "환경" if blocked else None,
                "retry_verdict": stages.parse_verify_verdict(rtext) if rtext else None,
                "research_cost": _cost_of(research), "plan_cost": plan_cost,
                "rp_cost": round(sum(_cost_of(rec) for rec in rp_recs), 6),
                "retry_total_cost": round(sum(c for c in map(_cost_of, counted)
                                              if c is not None), 6),
                "rp_io": _tokens_of(rp_recs, "io"),
                "rp_quota": _tokens_of(rp_recs, "quota"),
                "retry_io": _tokens_of(counted, "io"),
                "retry_quota": _tokens_of(counted, "quota"),
                "retry_stages": _run_stage_calls(r),
            })

    cost = _share_summary([p["rp_cost"] for p in pairs],
                          [p["retry_total_cost"] for p in pairs])
    tokens = {}
    for basis in ("io", "quota"):
        t = _share_summary([p[f"rp_{basis}"] for p in pairs],
                           [p[f"retry_{basis}"] for p in pairs])
        tokens[basis] = {"rp_sum": t["report_sum"], "retry_sum": t["pipeline_sum"],
                         "pooled_ratio": t["pooled_ratio"]}
    rp_sum = round(cost["report_sum"], 6)
    sample_spend = round(sample_spend, 6)
    return {
        "run_dirs_n": len(run_dirs), "n": len(pairs),
        "excluded": {k: excluded.get(k, 0)
                     for k in ("no_retry", "no_usage", "no_research_usage")},
        "excluded_pairs": excluded_pairs, "plan_missing": plan_missing,
        "rp_cost_sum": rp_sum, "retry_cost_sum": round(cost["pipeline_sum"], 6),
        "pooled_ratio": cost["pooled_ratio"], "median_run_ratio": cost["median_run_ratio"],
        "tokens": tokens, "sample_spend": sample_spend,
        "spend_ratio": round(rp_sum / sample_spend, 4) if sample_spend else None,
        "min_n": VERIFY_FAIL_RETRY_MIN_N, "sufficient": len(pairs) >= VERIFY_FAIL_RETRY_MIN_N,
        "pairs": pairs,
    }


def _breakdown_lines(bd: dict) -> list[str]:
    """Render pipeline_breakdown() as the appended '스테이지별 분해' (per-stage
    breakdown) section.

    Purely additive to render(): the four sections above it (and write()/__main__)
    are unchanged and pinned by tests, because the todo-sync daemon regenerates
    Metrics.md and must never be broken by this block."""
    order = list(stages.STAGES_AUTO)   # research, plan, implement, verify, report
    lines = [
        "## 스테이지별 분해 (pipeline breakdown)",
        f"> 라우팅·rework 는 렛저 전수(**{bd['ledger_n']}건**), 스테이지 콜/바이트/판정은 "
        f"디스크 run_dir 표본(**{bd['run_dirs_n']}개**; `Worker/runs/` 는 gitignored·휘발이라 "
        "전체 이력이 아니다).",
        "",
        "### 라우팅 분포 (worker_mode × 결과)",
    ]
    for mo in sorted(bd["routing_totals"], key=lambda m: -bd["routing_totals"][m]):
        detail = ", ".join(f"{s} {n}" for s, n in sorted(bd["routing"][mo].items()))
        lines.append(f"- **{mo}**: {bd['routing_totals'][mo]} ({detail})")
    lines += ["", "### 스테이지별 콜 수 · 재입력량(아티팩트 바이트)"]
    for st in order + sorted(set(bd["stage_calls"]) - set(order)):
        lines.append(f"- {st}: 콜 {bd['stage_calls'].get(st, 0)} · "
                     f"{bd['stage_bytes'].get(st, 0):,} bytes")
    verdicts = " · ".join(f"{k} {v}" for k, v in sorted(bd["verify_verdicts"].items()))
    rd = bd["redispatch"]
    rw = bd["rework"]
    lines += [
        "", "### verify 판정 tally", f"- {verdicts or '없음'}",
        "", "### 재디스패치 다중도 · rework",
        f"- run_dir {rd['run_dirs']} / 고유 slug {rd['distinct_slugs']} / "
        f"2회 이상 실행 태스크 {rd['redispatched_slugs']}",
        f"- 렛저 rework: 재시도(attempts>1) {rw['retried']} · refail>0 {rw['refailed']} · "
        f"총 refail {rw['refail_total']} · ci-fix {rw['ci_fix']}",
        "",
    ]
    lines += _usage_lines(bd, order)
    return lines


def _num(v) -> str:
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"


def _usd(v: float) -> str:
    return f"${v:,.2f}" if v >= 0.01 else f"${v:.4f}"


def _usage_lines(bd: dict, order: list[str]) -> list[str]:
    """The '스테이지별 토큰 usage' (per-stage token usage) table. Tolerates a breakdown without
    `stage_usage` (one that predates it) — a raise here would cost render() the
    whole breakdown section."""
    su = bd.get("stage_usage") or {}
    rows = su.get("stages") or {}
    lines = ["### 스테이지별 토큰 usage (stages.json `usage`)"]
    if not rows:
        return lines + [f"- 아직 usage 가 기록된 run_dir 이 없다 "
                        f"(표본 run_dir {bd.get('run_dirs_n', 0)}개 전부 계측 이전)", ""]
    keys = ("quota_tokens", *stages.USAGE_TOKEN_KEYS)
    lines += [
        f"> run_dir {bd.get('run_dirs_n', 0)}개 중 usage 보유 {su.get('runs_with_usage', 0)}개 "
        "— 계측 이전 run_dir 은 표본 제외. 쿼터 = input + cache_creation + output "
        "(cache_read 제외). 비용은 CLI 가 API 정가로 환산한 추정치이지 청구액이 아니다.",
        "",
        "| stage | N | unavailable | 쿼터 Σ (중앙값) | input Σ (중앙값) | output Σ (중앙값) "
        "| cache_creation Σ (중앙값) | cache_read Σ (중앙값) | 비용 추정 Σ |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for st in order + sorted(set(rows) - set(order)):
        r = rows.get(st)
        if r is None:
            continue
        cells = [f"{_num(r['sum'][k])} ({_num(r['median'][k])})" if r["n"] else "—"
                 for k in keys]
        cost = "—"
        if r.get("cost_usd") is not None:
            cost = _usd(r["cost_usd"])
            if r.get("cost_n", r["n"]) < r["n"]:
                cost += f" ({r['cost_n']}/{r['n']})"
        lines.append(f"| {st} | {r['n']} | {r['unavailable']} | {' | '.join(cells)} | {cost} |")
    return lines + [""]


def render() -> str:
    led = _load_ledger()
    st = lambda s: [a for a in led if a.get("state") == s]  # noqa: E731
    # A deferred row (FAILED + checkout_busy/job_running) never ran or is still
    # running: counting it would inflate the failure rate with healthy waits.
    done = st("done")
    failed = [a for a in st("failed") if not is_deferred(a)]
    active = [a for a in led if a.get("state") in ("dispatched", "accepted", "in_progress")]
    retried = [a for a in led if int(a.get("attempts", 1) or 1) > 1]
    refail = [a for a in led if int(a.get("refail_count", 0) or 0) > 0]
    refail_total = sum(int(a.get("refail_count", 0) or 0) for a in led)

    db = _db_counts()
    stage_runs = len(glob.glob(str(RUNS / "*" / "*.md")))   # ~1 claude -p per stage artifact
    run_dirs = len(glob.glob(str(RUNS / "*")))
    ci_fix = sum(1 for a in led if "ci-fix" in a.get("task_id", ""))
    reverts = _reverts_in_vault()

    # rough proxies
    stages_per_task = (stage_runs / run_dirs) if run_dirs else 0
    rework = ci_fix + reverts
    rework_ratio = (rework / db["done"]) if db["done"] else 0
    fail_ratio = (len(failed) / (len(done) + len(failed))) if (done or failed) else 0

    out = [
        "---", "tags: [orchestrator, metrics]", f"updated: {m.now_iso()}", "---",
        "", "# 📊 자율 시스템 메트릭", "",
        "> 이 시스템이 실제로 남는 장사인지 보는 지표. rework율이 핵심 품질 신호.",
        "",
        "## 처리량 (throughput)",
        f"- todo done: **{db['done']}** (auto/delegate: {db['auto_done']}, cancelled: {db['cancelled']})",
        f"- 렛저 done: {len(done)} · 실패: {len(failed)} · 진행중: {len(active)}",
        "",
        "## 실행 신뢰성 (얼마나 매끄럽게 되나)",
        f"- 재시도 필요(attempts>1): {len(retried)}",
        f"- 실패 후 재dispatch된 태스크: {len(refail)} (총 재dispatch {refail_total}회)",
        f"- 실패율(failed/(done+failed)): **{fail_ratio:.0%}**",
        "",
        "## 비용 proxy",
        f"- 스테이지 실행(≈ claude -p 콜): **{stage_runs}** (run_dir {run_dirs}개, 태스크당 ~{stages_per_task:.1f} 스테이지)",
        "  · 단발 실행이면 태스크당 1콜 → 5스테이지는 ~5배. right-sizing 여지.",
        "",
        "## rework proxy (품질 신호 — 낮을수록 좋음)",
        f"- ci-fix 태스크(직전 작업이 CI 깬 흔적): {ci_fix}",
        f"- vault revert(되돌린 머지): {reverts}",
        f"- **rework율 ≈ {rework_ratio:.0%}** ((ci-fix+revert)/done)",
        "  · 외부 repo의 rework(머지 후 수정)는 여기서 미측정 — 별도 추적 필요.",
        "",
        "## 해석",
        f"- 실패율 {fail_ratio:.0%} · rework {rework_ratio:.0%} · 태스크당 {stages_per_task:.1f}스테이지.",
        "- 실패율↑이면 flake/모델품질, rework↑이면 검증부족(CI만으론 부족), 스테이지수↑이면 비용과잉.",
        "",
    ]
    # Append the per-stage breakdown. Best-effort: this block must never be able
    # to break Metrics.md regeneration (the daemon depends on write()/render()),
    # so any failure degrades to the four sections above rather than raising.
    try:
        out.extend(_breakdown_lines(pipeline_breakdown()))
    except Exception:
        pass
    return "\n".join(out)


def write(out_path: str | Path = DEFAULT_OUT) -> str:
    text = render()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(text, encoding="utf-8")
    return text


if __name__ == "__main__":
    print(write())
