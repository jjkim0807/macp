"""Evaluation-harness tests for ``todo_system.orchestrator.eval_memory`` (memory v2 · G4).

Pins the §5 retrieval hit-rate harness (``docs/design/graph-memory.md`` §5.1–5.4):
precision@k / recall@k (k∈{5,10}) for the **v1** baseline (``get_related`` BFS +
``most_referenced_slugs`` global padding, the index.md "top-10 집계") against the
**v2** hybrid (``query_related``), measured over the **same** label set and rendered
as the §5.2 comparison table.

The four behaviours the design makes load-bearing map onto the cases below:

* **metric math** (§5.1) — precision@k denominator is **k**, recall@k denominator is
  **|relevant|**; a labeled task with an empty ``relevant`` is excluded from both
  averages (it would otherwise drag every score toward zero).
* **v1 is a fair, non-strawman baseline** (O.Q.4) — candidates are the ``get_related``
  BFS endpoints ranked ``(hop asc, -global_refcount, slug asc)`` and then **padded**
  with the global ``most_referenced_slugs`` top entries a real Research read surfaces.
* **weak label + 권장 gating** (AC#1/AC#2) — the table carries a "약한 라벨(참고)"
  caption until a human golden set is confirmed, and the wiring is marked 권장
  **only when** v2 is actually superior (recall@10 up, precision@5 not down);
  measurement may refute the hypothesis, so superiority is reported, never asserted.
* **reviewable emit** (AC#1) — ``emit_candidates`` produces a ``status="weak"`` scaffold
  with a per-candidate ``why``, and ``load_labelset`` round-trips its ``relevant`` list.

Isolation mirrors the G1–G3 files: library-level cases drive the functions against an
``isolated_vault`` with an explicit ``con=``; the semantic-layer case is
``importorskip``-guarded; fixtures use only fake slugs / relative paths / placeholder
provenance (public repo — no real name/token/host/abs-home path).
"""

from __future__ import annotations

import pytest

from todo_system import graph_memory as gm
from todo_system.orchestrator import eval_memory as em

# Placeholder provenance — fake values, never a real identifier (G1 convention).
FAKE_RUN_ID = "20260806T000000-fake-run"
FAKE_COMMIT_SHA = "0" * 40
FIXED_RECORDED_AT = "2026-08-06T00:00:00"


def _onehot_embedder(text: str) -> list[float]:
    """Tiny deterministic fake embedder: a one-hot 3-vector keyed by a fruit word, so
    the nearest-neighbour answer for a crafted query is predictable (G2/G3 idiom)."""
    t = text.lower()
    if "apple" in t:
        return [1.0, 0.0, 0.0]
    if "banana" in t:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


def _edge(con, from_slug, to_slug, *, edge_type="depends_on", run_id=None, commit_sha=None):
    """Insert one current-valid edge with a fixed ``recorded_at`` (so every fixture
    edge is live, ``valid_until IS NULL``) and chosen provenance."""
    gm.insert_edges(
        con,
        [
            {
                "from_slug": from_slug,
                "to_slug": to_slug,
                "edge_type": edge_type,
                "source_file": f"Todo/items/{from_slug}.md",
                "event_date": None,
                "recorded_at": FIXED_RECORDED_AT,
                "valid_until": None,
                "run_id": run_id,
                "commit_sha": commit_sha,
            }
        ],
    )


def _build_v1_graph(con):
    """A small deterministic graph that pins v1_query's ``(hop, -refcount, slug)``
    order plus global padding:

      - ``task-main → node-a``                 (hop 1, refcount 1)
      - ``task-main → node-b`` **twice**       (hop 1, refcount 2 — two edges raise
                                                node-b's refcount with no new node)
      - ``node-a → node-c``                    (hop 2, refcount 1)
      - ``glob-{x,y,z} → node-pop``            (refcount 3, **unreachable** from
                                                task-main → only global padding finds it)
    """
    _edge(con, "task-main", "node-a")
    _edge(con, "task-main", "node-b", edge_type="depends_on")
    _edge(con, "task-main", "node-b", edge_type="related_tasks")
    _edge(con, "node-a", "node-c")
    _edge(con, "glob-x", "node-pop")
    _edge(con, "glob-y", "node-pop")
    _edge(con, "glob-z", "node-pop")


# ---------------------------------------------------------------------------
# metric math (design §5.1)
# ---------------------------------------------------------------------------


def test_precision_recall_math():
    ranked = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
    relevant = {"a", "c", "f", "x"}  # "x" never retrieved; |relevant| = 4

    # precision@k denominator = k (design §5.1), NOT |results|.
    assert em.precision_at_k(ranked, relevant, 5) == pytest.approx(2 / 5)  # {a,c} / 5
    assert em.precision_at_k(ranked, relevant, 10) == pytest.approx(3 / 10)  # {a,c,f} / 10
    # recall@k denominator = |relevant|.
    assert em.recall_at_k(ranked, relevant, 5) == pytest.approx(2 / 4)  # {a,c} / 4
    assert em.recall_at_k(ranked, relevant, 10) == pytest.approx(3 / 4)  # {a,c,f} / 4


def test_metrics_skip_empty_relevant():
    labelset = em.LabelSet(
        status="golden",
        labels=[
            em.LabelEntry(task={"slug": "t1"}, relevant=["a", "b"], label_source="golden"),
            em.LabelEntry(task={"slug": "t2"}, relevant=[], label_source="golden"),
        ],
    )
    per_task_results = [(["a", "b", "c"], 1.0), (["z"], 2.0)]
    m = em.compute_metrics(per_task_results, labelset, ks=(5, 10))

    # Only t1 (non-empty relevant) contributes to precision/recall: p@5 = 2/5, not
    # diluted toward 1/5 by averaging in t2's empty-relevant zero.
    assert m.precision_at[5] == pytest.approx(2 / 5)
    assert m.precision_at[10] == pytest.approx(2 / 10)
    assert m.recall_at[5] == pytest.approx(1.0)
    assert m.recall_at[10] == pytest.approx(1.0)
    # latency median spans every queried task (a system property, label-independent).
    assert m.latency_p50_ms == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# v1 baseline ranking (O.Q.4: BFS hop → global refcount → slug, + global padding)
# ---------------------------------------------------------------------------


def test_v1_ranking_bfs_plus_refcount_and_pad(isolated_vault):
    con = isolated_vault.connect()
    try:
        _build_v1_graph(con)
        task = {"slug": "task-main"}

        # hop-1 ordered by refcount (node-b=2 before node-a=1), then hop-2 (node-c),
        # then global padding pulls in node-pop (refcount 3, structurally unreachable).
        ranked = em.v1_query(con, task, 5, depth=2, pad_with_global=True)
        assert ranked == ["node-b", "node-a", "node-c", "node-pop"]

        # padding off → only the structural neighbours, same order, node-pop absent.
        assert em.v1_query(con, task, 5, depth=2, pad_with_global=False) == [
            "node-b",
            "node-a",
            "node-c",
        ]

        # truncation to k keeps the ranked prefix.
        assert em.v1_query(con, task, 2, depth=2) == ["node-b", "node-a"]
    finally:
        con.close()


# ---------------------------------------------------------------------------
# v2 hybrid delegation (semantic KNN + structural BFS)
# ---------------------------------------------------------------------------


def test_v2_query_delegates_to_query_related(isolated_vault):
    pytest.importorskip("sqlite_vec")
    isolated_vault.write("Notes/apple-doc.md", "apple orchard harvest season\n")
    con = isolated_vault.connect()
    try:
        gm.embed_vault(con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3)
        _edge(con, "task-main", "struct-neighbor")

        task = {"slug": "task-main", "title": "apple harvest planning"}
        got = em.v2_query(con, task, 10, embedder=_onehot_embedder, dim=3)

        # delegates to query_related: identical ranked slugs, truncated to k.
        expected = [
            it.slug
            for it in gm.query_related(con, task, k=10, embedder=_onehot_embedder, dim=3)
        ][:10]
        assert got == expected
        # and it really blended both layers: structural + semantic hits both present.
        assert {"struct-neighbor", "apple-doc"} <= set(got)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# end-to-end eval on the BFS-only fallback (no sqlite-vec needed)
# ---------------------------------------------------------------------------


def test_eval_emits_table_on_bfs_only_fallback(isolated_vault):
    con = isolated_vault.connect()
    try:
        _build_v1_graph(con)
        assert not gm._table_exists(con, "vec_embeddings")  # BFS-only query path

        labelset = em.LabelSet(
            status="weak",
            labels=[
                em.LabelEntry(
                    task={"slug": "task-main", "title": "no fruit here"},
                    relevant=["node-b", "node-c"],
                    label_source="weak",
                ),
            ],
        )
        m_v1, m_v2 = em.run_eval(con, labelset, ks=(5, 10))

        # both rows computed, every metric in range, even with the semantic layer off.
        for m in (m_v1, m_v2):
            assert set(m.precision_at) == {5, 10}
            assert set(m.recall_at) == {5, 10}
            assert 0.0 <= m.precision_at[5] <= 1.0
            assert 0.0 <= m.recall_at[10] <= 1.0

        table = em.render_table(m_v1, m_v2, weak=True)
        assert table.strip()
        assert "v1" in table and "v2" in table
        assert "precision@5" in table and "recall@10" in table
    finally:
        con.close()


# ---------------------------------------------------------------------------
# weak-label caption (AC#1) + 권장 verdict gating (AC#2)
# ---------------------------------------------------------------------------


def test_weak_label_annotation():
    m_v1 = em.Metrics(
        precision_at={5: 0.4, 10: 0.3}, recall_at={5: 0.5, 10: 0.6}, latency_p50_ms=1.0
    )
    m_v2 = em.Metrics(
        precision_at={5: 0.4, 10: 0.35}, recall_at={5: 0.55, 10: 0.75}, latency_p50_ms=2.0
    )

    weak = em.render_table(m_v1, m_v2, weak=True)
    assert "약한 라벨(참고)" in weak

    strong = em.render_table(m_v1, m_v2, weak=False)
    assert "약한 라벨(참고)" not in strong


def test_verdict_recommends_only_when_v2_superior():
    base = em.Metrics(
        precision_at={5: 0.4, 10: 0.3}, recall_at={5: 0.5, 10: 0.6}, latency_p50_ms=1.0
    )

    # recall@10 up (0.75 > 0.6) AND precision@5 not down (0.4 == 0.4) → 권장.
    better = em.Metrics(
        precision_at={5: 0.4, 10: 0.35}, recall_at={5: 0.55, 10: 0.75}, latency_p50_ms=2.0
    )
    recommended, reason = em.verdict(base, better)
    assert recommended is True
    assert reason  # a one-line human-readable rationale always accompanies the verdict

    # recall@10 up but precision@5 down (0.3 < 0.4) → 미권장.
    precision_regressed = em.Metrics(
        precision_at={5: 0.3, 10: 0.2}, recall_at={5: 0.4, 10: 0.75}, latency_p50_ms=2.0
    )
    assert em.verdict(base, precision_regressed)[0] is False

    # recall@10 flat (equal) → 미권장 even with precision up (hybrid earns nothing).
    recall_flat = em.Metrics(
        precision_at={5: 0.5, 10: 0.4}, recall_at={5: 0.6, 10: 0.6}, latency_p50_ms=2.0
    )
    assert em.verdict(base, recall_flat)[0] is False


# ---------------------------------------------------------------------------
# reviewable weak-label scaffold + round-trip (AC#1)
# ---------------------------------------------------------------------------


def test_emit_candidates_scaffold_and_roundtrip(isolated_vault):
    con = isolated_vault.connect()
    try:
        _build_v1_graph(con)
        tasks = [{"slug": "task-main", "title": "no fruit here"}]

        scaffold = em.emit_candidates(con, tasks, k=5)
        assert scaffold["version"] == 1
        assert scaffold["status"] == "weak"
        assert "_note" in scaffold  # self-describing: why these labels are weak

        entry = scaffold["labels"][0]
        cands = entry["candidate_relevant"]
        assert cands  # non-empty: structural neighbours were found
        for c in cands:
            assert c["slug"]
            assert c["why"]  # every candidate carries a human-readable reason

        # relevant is pre-seeded = candidate slugs (the human prunes before confirming).
        assert entry["relevant"] == [c["slug"] for c in cands]
        assert entry["label_source"] == "weak"

        # load_labelset round-trips relevant, ignoring candidate_relevant / _note.
        ls = em.load_labelset(scaffold)
        assert ls.status == "weak"
        assert ls.labels[0].relevant == [c["slug"] for c in cands]
        assert ls.labels[0].task["slug"] == "task-main"
    finally:
        con.close()
