"""Retrieval hit-rate evaluation harness for graph memory v2 (design §5, leaf G4).

Measures whether the hybrid v2 query (:func:`todo_system.graph_memory.query_related`)
actually retrieves more of the "관련 과거 실패·결정" than the v1 baseline it replaces,
over the **same** label set, and renders the design §5.2 comparison table — so a
"고차원" claim is backed by numbers instead of asserted (design §5; the measurement
is allowed to **refute** the hypothesis).

Two retrieval surfaces, one label set
-------------------------------------
* **v1** (:func:`v1_query`) — the current baseline: structural ``get_related`` BFS
  from the task slug, ranked ``(hop asc, -global_refcount, slug asc)`` and then padded
  up to ``k`` with the global ``most_referenced_slugs`` top entries (the index.md
  "top-10 집계" a real Research read surfaces). Padding makes v1 a **fair, non-strawman**
  baseline — it strengthens v1, so the v2-superiority bar stays conservative.
* **v2** (:func:`v2_query`) — thin delegation to ``query_related`` (semantic KNN +
  structural BFS), with the same graceful BFS-only fallback when sqlite-vec is absent.

Metrics (design §5.1): precision@k = ``|R∩relevant| / k``, recall@k =
``|R∩relevant| / |relevant|``, averaged over the labeled tasks whose ``relevant`` set
is non-empty, for ``k ∈ {5, 10}``, plus query-latency p50.

Labels (design §5.1/§5.4): a human **golden set** is primary. Until it is confirmed,
:func:`emit_candidates` writes a **reviewable weak-label scaffold** (``status="weak"``,
per-candidate ``why``) and the rendered table carries a "약한 라벨(참고)" caption; the
wiring is marked **권장** only when :func:`verdict` finds v2 genuinely superior.

This module **imports** ``graph_memory`` and queries it; it changes no engine schema,
embedding or query code (those are out of G4 ``touches``). Stdlib only, no top-level
``sqlite_vec`` import, and no hardcoded vault path — the DB is resolved from
``--vault-root`` / the module globals, like ``graph_memory``'s own CLI.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .. import graph_memory as gm

# Upper bound on distinct ``to_slug`` refcounts pulled from ``most_referenced_slugs``
# for v1 ranking + padding. Comfortably above the current vault's distinct-target
# count; a larger graph simply ranks/pads from the top ``_GLOBAL_REF_LIMIT`` targets.
_GLOBAL_REF_LIMIT = 10_000


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelEntry:
    """One labeled task: its identity mapping (``slug`` + ``title`` + ``touches`` …),
    the ``relevant`` past-record slugs, and whether the label is ``golden`` or ``weak``."""

    task: Mapping[str, Any]
    relevant: list[str]
    label_source: str = "golden"


@dataclass(frozen=True)
class LabelSet:
    """A label set ``L`` = ``{(task_i, relevant_i)}`` plus its overall ``status``
    (``golden`` once a human has confirmed it, else ``weak``)."""

    status: str
    labels: list[LabelEntry]


@dataclass(frozen=True)
class Metrics:
    """Averaged hit-rate for one retrieval surface over a label set, keyed by ``k``,
    plus the query-latency p50 in milliseconds."""

    precision_at: dict[int, float]
    recall_at: dict[int, float]
    latency_p50_ms: float


# ---------------------------------------------------------------------------
# Metric math (design §5.1)
# ---------------------------------------------------------------------------


def precision_at_k(results: list[str], relevant: Any, k: int) -> float:
    """``|set(results[:k]) & relevant| / k`` — the fraction of the top-``k`` returned
    that is relevant. Denominator is **k** (design §5.1), never ``|results|``."""
    if k <= 0:
        return 0.0
    rel = set(relevant)
    hits = len(set(results[:k]) & rel)
    return hits / k


def recall_at_k(results: list[str], relevant: Any, k: int) -> float:
    """``|set(results[:k]) & relevant| / |relevant|`` — the fraction of what should
    have been found that the top-``k`` actually found. 0.0 for an empty ``relevant``."""
    rel = set(relevant)
    if not rel:
        return 0.0
    hits = len(set(results[:k]) & rel)
    return hits / len(rel)


def compute_metrics(
    per_task_results: list[tuple[list[str], float]],
    labelset: LabelSet,
    *,
    ks: tuple[int, ...] = (5, 10),
) -> Metrics:
    """Average precision@k / recall@k over the labeled tasks, carrying latency p50.

    ``per_task_results`` is parallel to ``labelset.labels``: each entry is
    ``(ranked_slugs, latency_ms)``. A task whose ``relevant`` set is empty is excluded
    from the precision/recall averages (design §5.1 — otherwise it drags every score
    toward zero), but its latency still counts, since latency is a system property
    independent of the labels.
    """
    precision_sums = {k: 0.0 for k in ks}
    recall_sums = {k: 0.0 for k in ks}
    latencies: list[float] = []
    scored = 0
    for (ranked, latency_ms), entry in zip(per_task_results, labelset.labels):
        latencies.append(float(latency_ms))
        relevant = set(entry.relevant)
        if not relevant:
            continue
        scored += 1
        for k in ks:
            precision_sums[k] += precision_at_k(ranked, relevant, k)
            recall_sums[k] += recall_at_k(ranked, relevant, k)
    precision_at = {k: (precision_sums[k] / scored if scored else 0.0) for k in ks}
    recall_at = {k: (recall_sums[k] / scored if scored else 0.0) for k in ks}
    latency_p50 = statistics.median(latencies) if latencies else 0.0
    return Metrics(precision_at=precision_at, recall_at=recall_at, latency_p50_ms=latency_p50)


# ---------------------------------------------------------------------------
# v1 baseline + v2 hybrid retrieval surfaces
# ---------------------------------------------------------------------------


def _edge_endpoints(rows: list[dict[str, Any]]) -> set[str]:
    """The ``from_slug`` / ``to_slug`` endpoints across a ``get_related`` edge list."""
    out: set[str] = set()
    for r in rows:
        out.add(r["from_slug"])
        out.add(r["to_slug"])
    return out


def v1_query(
    con: Any,
    task: Mapping[str, Any],
    k: int,
    *,
    depth: int = 2,
    pad_with_global: bool = True,
) -> list[str]:
    """The v1 baseline ranking built **only** from ``get_related`` + ``most_referenced_slugs``.

    Candidates are the structural BFS endpoints of the task slug (``get_related``),
    the task's own slug excluded, ranked by ``(hop asc, -global_refcount, slug asc)``
    where ``hop`` is 1 for the ``depth=1`` neighbour set and 2 for anything reached
    only within ``depth`` hops (the public surface distinguishes no finer). The list
    is then **padded** up to ``k`` with the global ``most_referenced_slugs`` top
    entries not already present — the task-independent index.md "top-10 집계" a real
    Research read sees — making v1 a fair, non-strawman baseline.
    """
    slug = task.get("slug")
    ordered_global = gm.most_referenced_slugs(con, limit=_GLOBAL_REF_LIMIT)
    refcount = dict(ordered_global)

    ranked: list[str] = []
    if slug:
        hop1 = _edge_endpoints(gm.get_related(con, slug, depth=1)) - {slug}
        deep = _edge_endpoints(gm.get_related(con, slug, depth=max(depth, 1))) - {slug}
        hop: dict[str, int] = {s: 1 for s in hop1}
        for s in deep - hop1:
            hop[s] = 2
        ranked = sorted(hop, key=lambda s: (hop[s], -refcount.get(s, 0), s))

    if pad_with_global and len(ranked) < k:
        present = set(ranked)
        if slug:
            present.add(slug)
        for cand, _n in ordered_global:
            if len(ranked) >= k:
                break
            if cand in present:
                continue
            ranked.append(cand)
            present.add(cand)

    return ranked[:k]


def v2_query(con: Any, task: Mapping[str, Any], k: int, **kw: Any) -> list[str]:
    """The v2 hybrid ranking: the slugs of ``query_related`` (semantic KNN + structural
    BFS), truncated to ``k``. Delegates entirely — it adds no scoring of its own — and
    inherits the graceful BFS-only fallback when sqlite-vec is unavailable."""
    return [it.slug for it in gm.query_related(con, task, k=k, **kw)][:k]


def run_eval(
    con: Any,
    labelset: LabelSet,
    *,
    ks: tuple[int, ...] = (5, 10),
    v1_kwargs: dict[str, Any] | None = None,
    v2_kwargs: dict[str, Any] | None = None,
) -> tuple[Metrics, Metrics]:
    """Query v1 and v2 for every labeled task (timing each), then compute both
    :class:`Metrics`. Each task is queried at ``max(ks)`` so one ranked list answers
    every requested ``k``. Returns ``(metrics_v1, metrics_v2)``."""
    max_k = max(ks)
    v1_results: list[tuple[list[str], float]] = []
    v2_results: list[tuple[list[str], float]] = []
    for entry in labelset.labels:
        task = entry.task
        t0 = time.perf_counter()
        r1 = v1_query(con, task, max_k, **(v1_kwargs or {}))
        t1 = time.perf_counter()
        r2 = v2_query(con, task, max_k, **(v2_kwargs or {}))
        t2 = time.perf_counter()
        v1_results.append((r1, (t1 - t0) * 1000.0))
        v2_results.append((r2, (t2 - t1) * 1000.0))
    m_v1 = compute_metrics(v1_results, labelset, ks=ks)
    m_v2 = compute_metrics(v2_results, labelset, ks=ks)
    return m_v1, m_v2


# ---------------------------------------------------------------------------
# Verdict + §5.2 table
# ---------------------------------------------------------------------------


def verdict(m_v1: Metrics, m_v2: Metrics) -> tuple[bool, str]:
    """Is the v2 wiring worth recommending? True iff recall@10 strictly improves **and**
    precision@5 does not regress (design §5.2 판정 규칙: a hybrid that cannot lift at
    least recall, without giving up precision, earns nothing). Returns
    ``(recommended, one_line_reason)`` — never asserted, only reported (AC#2)."""
    v2_r10 = m_v2.recall_at.get(10, 0.0)
    v1_r10 = m_v1.recall_at.get(10, 0.0)
    v2_p5 = m_v2.precision_at.get(5, 0.0)
    v1_p5 = m_v1.precision_at.get(5, 0.0)
    recommended = (v2_r10 > v1_r10) and (v2_p5 >= v1_p5)
    if recommended:
        reason = (
            f"v2 recall@10 {v2_r10:.3f} > v1 {v1_r10:.3f} 이고 "
            f"precision@5 {v2_p5:.3f} ≥ v1 {v1_p5:.3f} — 하이브리드가 값어치를 함"
        )
    else:
        reason = (
            f"v2 가 v1 을 유의하게 넘지 못함 (recall@10 {v2_r10:.3f} vs {v1_r10:.3f}, "
            f"precision@5 {v2_p5:.3f} vs {v1_p5:.3f}) — 측정이 가설을 반박 (설계 §5.2)"
        )
    return recommended, reason


_V1_LABEL = "**v1** (현행: get_related BFS + index.md 집계, wikilink/frontmatter 문자열 매칭)"
_V2_LABEL = "**v2** (임베딩 KNN + 그래프 BFS 하이브리드, R3)"
_INDEX_COST_V1 = "매 틱 전체 재빌드 (clear_edges + 전체 DELETE)"
_INDEX_COST_V2 = "증분 (append + tombstone)"
_WEAK_CAPTION = (
    "> ⚠ 약한 라벨(참고) — 사람 골든셋 확정 전, 자동 후보 라벨로 계산한 결과다. 설계 §5.1 상 "
    "사람 골든셋이 1차이므로 아래 숫자는 참고용이며 확정 라벨로 재실행해야 한다."
)


def _row(label: str, m: Metrics, index_cost: str) -> str:
    return (
        f"| {label} "
        f"| {m.precision_at.get(5, 0.0):.3f} | {m.precision_at.get(10, 0.0):.3f} "
        f"| {m.recall_at.get(5, 0.0):.3f} | {m.recall_at.get(10, 0.0):.3f} "
        f"| {m.latency_p50_ms:.1f} | {index_cost} |"
    )


def render_table(m_v1: Metrics, m_v2: Metrics, *, weak: bool) -> str:
    """Render the design §5.2 comparison table (v1 vs v2) as markdown, with a
    "약한 라벨(참고)" caption when ``weak`` and a 권장/미권장 footer from :func:`verdict`."""
    lines: list[str] = []
    if weak:
        lines.append(_WEAK_CAPTION)
        lines.append("")
    lines.append(
        "| 방식 | precision@5 | precision@10 | recall@5 | recall@10 "
        "| 질의 지연 p50(ms) | 색인 비용 |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(_row(_V1_LABEL, m_v1, _INDEX_COST_V1))
    lines.append(_row(_V2_LABEL, m_v2, _INDEX_COST_V2))
    recommended, reason = verdict(m_v1, m_v2)
    lines.append("")
    lines.append(f"**배선 제안: {'권장' if recommended else '미권장'}** — {reason}")
    return "\n".join(lines) + "\n"
