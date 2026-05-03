"""Parity checks for mnemos_hot v0.2 optional accelerators.

These tests run only when the local Rust wheel is installed. The
default MNEMOS install remains wheel-optional; call-site tests with
fake modules cover opt-in dispatch when the wheel is absent.
"""
from __future__ import annotations

import pytest

from mnemos.domain.compression.contest_store import _sha256_batch_python
from mnemos.domain.compression.judge import _judge_deterministic_score_python
from mnemos.domain.compression.quality_analyzer import _normalize_embeddings_python
from mnemos.persistence.postgres import _rerank_composite_python

mnemos_hot = pytest.importorskip("mnemos_hot")


def _assert_pairs_close(left, right, *, abs_tol: float = 1e-9) -> None:
    assert [idx for idx, _score in left] == [idx for idx, _score in right]
    assert [score for _idx, score in left] == pytest.approx(
        [score for _idx, score in right],
        abs=abs_tol,
    )


def test_judge_deterministic_score_matches_python_fallback():
    reference = "Alice joined Acme as a senior engineer last week."
    candidate = "Alice joined Acme as an engineer last week."

    expected = _judge_deterministic_score_python(reference, candidate)
    actual = mnemos_hot.judge_deterministic_score(reference, candidate, None)

    assert actual["bigram_overlap"] == pytest.approx(expected["bigram_overlap"], abs=1e-9)
    assert actual["edit_distance_ratio"] == pytest.approx(
        expected["edit_distance_ratio"],
        abs=1e-9,
    )
    assert actual["length_ratio"] == pytest.approx(expected["length_ratio"], abs=1e-9)
    assert actual["composite"] == pytest.approx(expected["composite"], abs=1e-9)


def test_normalize_embeddings_matches_python_fallback():
    vectors = [[3.0, 4.0], [0.0, 0.0], [-5.0, 12.0]]

    expected = _normalize_embeddings_python(vectors)
    actual = mnemos_hot.normalize_embeddings(vectors)

    assert len(actual) == len(expected)
    for actual_vector, expected_vector in zip(actual, expected):
        assert actual_vector == pytest.approx(expected_vector, abs=1e-9)


def test_rerank_composite_matches_python_fallback():
    query = [1.0, 0.0, 0.0]
    candidates = [
        [0.9, 0.1, 0.0],
        [0.8, 0.2, 0.0],
        [0.0, 1.0, 0.0],
        [1.0, 0.0, 0.0],
    ]
    recency_boost = [0.1, 0.5, 1.0, 0.0]

    expected = _rerank_composite_python(
        query, candidates, recency_boost, 0.85, 0.15, 3,
    )
    actual = mnemos_hot.rerank_composite(
        query, candidates, recency_boost, 0.85, 0.15, 3,
    )

    _assert_pairs_close(actual, expected)


def test_sha256_batch_matches_hashlib_fallback():
    payloads = [b"abc", b"", bytes(range(64))]

    expected = _sha256_batch_python(payloads)
    actual = mnemos_hot.sha256_batch(payloads)

    assert actual == expected
