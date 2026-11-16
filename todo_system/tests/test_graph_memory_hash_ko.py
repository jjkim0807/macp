"""``hash-ko`` embedder backend tests for ``todo_system.graph_memory`` (issue #146).

The default ``hash`` backend hashes whole ``\\w+`` tokens, so a Korean noun with
a particle attached (``논문을``) and the bare noun (``논문``) land in unrelated
buckets — ``'논문을 쓰는 일'`` vs ``'논문 작성'`` scores cosine 0.0. ``hash-ko``
keeps the same feature-hashing rule but, for every token containing a Hangul
syllable, also hashes the token's consecutive character bigrams at weight 0.7 in
the same key space.

Pinned here:

* registration — selectable via ``MACP_EMBEDDER`` and the ``.macp/config.toml``
  ``embedder`` key, same dim as ``hash``;
* the default ``hash`` vectors are byte-for-byte unchanged (digests captured on
  the tree *before* ``hash-ko`` was added);
* the AC pair: cosine 0.0 under ``hash``, > 0 under ``hash-ko``;
* text without Hangul embeds identically under both backends;
* exact bigram weighting, determinism across ``PYTHONHASHSEED``.

Pure stdlib — no sqlite-vec needed. Fixtures use only generic text.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import graph_memory as gm

_AC_A = "논문을 쓰는 일"
_AC_B = "논문 작성"

# sha256 over the little-endian float64 bytes of ``_default_embedder(text)``,
# captured on the unmodified tree before the hash-ko backend existed.
_HASH_PINS = {
    "논문을 쓰는 일": "a2fe7c241ae742b0af84c8f0ced1527f57ff2058c3a802842fe0204bc901802e",
    "논문 작성": "8a54b53e52ceb75abb32b4b3b238929310bfe37cae5ff44b91145ba7234713c0",
    "Refactor the graph memory embedder v2_index 2026": (
        "319a94c4dc2fc17eee6bc8d6aa1896946ae7b5500388411453709db49110ddb4"
    ),
}

_ENGLISH_SAMPLES = (
    "",
    "worker stage retried after a stale injected FAIL note",
    "Refactor the graph memory embedder v2_index 2026",
    "MixedCase, punctuation!? and   whitespace\ttabs\nnewlines",
    "snake_case_tokens 12345 0.5 a b c",
    "café naïve résumé — non-ASCII Latin but no Hangul",
)


def _cos(a, b) -> float:
    # Both embedders emit unit vectors (or the zero vector), so the dot product
    # is the cosine.
    return sum(x * y for x, y in zip(a, b))


def _vec_digest(vec) -> str:
    return hashlib.sha256(struct.pack(f"<{len(vec)}d", *vec)).hexdigest()


def _ref_embed(features: dict[str, float], dim: int = 256) -> list[float]:
    """Independent re-implementation of the sha1 bucket/sign rule with weights."""
    vec = [0.0] * dim
    for feat, weight in features.items():
        digest = hashlib.sha1(feat.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dim
        sign = 1.0 if digest[4] & 1 else -1.0
        vec[bucket] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm > 0 else vec


@pytest.fixture
def hash_fn():
    fn, _dim, _name = gm.resolve_embedder(name="hash")
    return fn


@pytest.fixture
def hash_ko_fn():
    fn, _dim, _name = gm.resolve_embedder(name="hash-ko")
    return fn


# ---------------------------------------------------------------------------
# AC#2 — the default backend is byte-for-byte unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", sorted(_HASH_PINS))
def test_default_hash_vectors_unchanged_bytewise(text):
    assert _vec_digest(gm._default_embedder(text)) == _HASH_PINS[text]


def test_default_backend_is_still_hash(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_EMBEDDER", raising=False)
    assert gm.EMBEDDER_DEFAULT == "hash"
    assert gm.embedder_name(start=tmp_path) == "hash"


# ---------------------------------------------------------------------------
# AC#1 — registration and selection
# ---------------------------------------------------------------------------


def test_hash_ko_registered(capsys):
    assert "hash-ko" in gm._EMBEDDER_BACKENDS
    assert gm.EMBEDDER_EXPECTED_DIMS["hash-ko"] == gm.EMBEDDER_EXPECTED_DIMS["hash"]
    assert gm.EMBEDDER_EXPECTED_DIMS["hash-ko"] == gm.EMBED_DIM

    fn, dim, name = gm.resolve_embedder(name="hash-ko")
    assert name == "hash-ko"
    assert dim == 256
    assert len(list(fn("논문"))) == 256
    assert "warning:" not in capsys.readouterr().out


def test_env_selects_hash_ko(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_EMBEDDER", "hash-ko")
    assert gm.embedder_name(start=tmp_path) == "hash-ko"
    _fn, dim, name = gm.resolve_embedder()
    assert (name, dim) == ("hash-ko", 256)

    monkeypatch.setenv("MACP_EMBEDDER", "HASH-KO")
    assert gm.embedder_name(start=tmp_path) == "hash-ko"


def test_config_key_selects_hash_ko(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_EMBEDDER", raising=False)
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text('embedder = "hash-ko"\n', encoding="utf-8")
    assert gm.embedder_name(start=tmp_path) == "hash-ko"


def test_unknown_name_error_lists_hash_ko(monkeypatch):
    monkeypatch.setenv("MACP_EMBEDDER", "nope")
    with pytest.raises(ValueError) as excinfo:
        gm.embedder_name()
    assert "hash-ko" in str(excinfo.value)


# ---------------------------------------------------------------------------
# AC#3 — the motivating pair, and English-only identity
# ---------------------------------------------------------------------------


def test_ac_pair_cosine(hash_fn, hash_ko_fn):
    assert _cos(hash_fn(_AC_A), hash_fn(_AC_B)) == 0.0
    assert _cos(hash_ko_fn(_AC_A), hash_ko_fn(_AC_B)) > 0.0


@pytest.mark.parametrize("text", _ENGLISH_SAMPLES)
def test_english_only_identical(hash_fn, hash_ko_fn, text):
    assert list(hash_ko_fn(text)) == list(hash_fn(text))


# ---------------------------------------------------------------------------
# Weighting rule
# ---------------------------------------------------------------------------


def test_bigram_weight_constant():
    assert gm.HASH_KO_BIGRAM_WEIGHT == 0.7


def test_bigram_weighting_exact(hash_ko_fn):
    expected = _ref_embed({"논문을": 1.0, "논문": 0.7, "문을": 0.7})
    assert list(hash_ko_fn("논문을")) == pytest.approx(expected, abs=1e-12)


def test_mixed_token_bigrams_span_all_chars(hash_ko_fn):
    # A token with any Hangul syllable gets bigrams over all its characters.
    expected = _ref_embed({"v2임베딩": 1.0, "v2": 0.7, "2임": 0.7, "임베": 0.7, "베딩": 0.7})
    assert list(hash_ko_fn("v2임베딩")) == pytest.approx(expected, abs=1e-12)


def test_single_char_hangul_token_has_no_bigram(hash_ko_fn, hash_fn):
    assert list(hash_ko_fn("일")) == list(hash_fn("일"))


def test_mixed_sentence_accumulates_per_token(hash_ko_fn):
    expected = _ref_embed({"graph": 1.0, "메모리": 1.0, "메모": 0.7, "모리": 0.7})
    assert list(hash_ko_fn("Graph 메모리")) == pytest.approx(expected, abs=1e-12)


# ---------------------------------------------------------------------------
# Determinism / normalisation
# ---------------------------------------------------------------------------


def test_hash_ko_deterministic_and_normalised(hash_ko_fn):
    text = "그래프 메모리에서 관련 문서를 찾는다"
    v1 = list(hash_ko_fn(text))
    v2 = list(hash_ko_fn(text))
    assert v1 == v2
    assert len(v1) == gm.EMBED_DIM
    assert abs(math.sqrt(sum(x * x for x in v1)) - 1.0) < 1e-9
    assert list(hash_ko_fn("")) == [0.0] * gm.EMBED_DIM


def test_hash_ko_stable_across_hash_seeds():
    code = (
        "import json; from todo_system import graph_memory as gm; "
        "fn, _d, _n = gm.resolve_embedder(name='hash-ko'); "
        "print(json.dumps(list(fn('논문을 쓰는 일 graph memory'))))"
    )
    repo_root = Path(__file__).resolve().parents[2]
    outs = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        res = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        outs.append(json.loads(res.stdout.strip().splitlines()[-1]))
    assert outs[0] == outs[1]


# ---------------------------------------------------------------------------
# Bench label
# ---------------------------------------------------------------------------


def test_bench_label_for_hash_ko():
    from todo_system.orchestrator import eval_memory as em

    assert "hash-ko" in em._BENCH_NAMES
