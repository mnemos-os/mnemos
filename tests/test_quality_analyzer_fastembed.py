"""Regression tests for the v4.2.0a11 fastembed refactor of QualityAnalyzer.

Two codex round-1 high-severity bugs in the original v4.2.0a11 commit:

1. **API misuse** — the new fastembed branch called
   ``self.embedding_model.encode(text)``. fastembed's API is
   ``embed(texts)``; ``.encode`` doesn't exist. Every comparison
   raised AttributeError, hit the broad ``except Exception``, and
   returned the conservative 85.0 default. Operators on ``[ml]`` /
   ``[gpu]`` / ``[phi]`` got a *constant* 85.0 across all comparisons —
   not embedding-driven similarity at all.

2. **Heuristic floor at 100%** — when neither fastembed nor any
   sentence-transformers fallback was loaded, the analyze() pipeline
   used ``semantic_similarity = 100.0`` as its default, then fed that
   into the weighted average at 0.4 weight. Unrelated text pairs with
   no shared entities or structure could end up at 100% quality
   simply because nobody had measured the semantic component. Silent
   over-rating.

Both fixed in the v4.2.0a11 round-2 patch:
* ``_compute_semantic_similarity`` now calls ``embed([text1, text2])``
  and returns ``-1.0`` as the "unavailable" sentinel (instead of 85.0
  as a non-failure-looking default).
* ``analyze()`` treats ``-1.0`` as "drop the semantic component" and
  reweights entity (0.5) + structure (0.5) without it. The
  ``content_preserved`` field in the manifest is None instead of a
  fake number.
"""
from __future__ import annotations

import asyncio
from typing import Iterable

import numpy as np

from mnemos.domain.compression.quality_analyzer import QualityAnalyzer


class _FakeFastEmbed:
    """Minimal fastembed stand-in.

    fastembed's real API: ``TextEmbedding(name).embed([texts])``
    returns an iterator of numpy arrays. We fake the same surface so
    the analyzer code talks to us through the actual public method
    rather than a side-channel that pre-fix code accidentally relied
    on.
    """

    def __init__(self, mapping: dict[str, np.ndarray] | None = None):
        # Map text → vector. Texts not in the map get a default zero
        # vector, which gives 0.0 cosine — useful for "unrelated"
        # cases.
        self._mapping = mapping or {}
        self._dim = next(iter(self._mapping.values())).shape[0] if mapping else 4

    def embed(self, texts: Iterable[str]) -> Iterable[np.ndarray]:
        for t in texts:
            if t in self._mapping:
                yield self._mapping[t]
            else:
                yield np.zeros(self._dim)


def _make_analyzer_with_fake_embed(mapping):
    """Construct an analyzer wired to a fake fastembed model."""
    analyzer = QualityAnalyzer.__new__(QualityAnalyzer)
    analyzer.enable_semantic_analysis = True
    analyzer.semantic_available = True
    analyzer.embedding_backend = "fastembed"
    analyzer.embedding_model = _FakeFastEmbed(mapping)
    return analyzer


def test_compute_semantic_similarity_uses_embed_not_encode():
    """The pre-fix code called ``.encode(...)`` which fastembed does
    not expose. This test pins that the call is now ``.embed([...])``
    by giving the fake a mapping it can hit only via embed()."""
    v_a = np.array([1.0, 0.0, 0.0, 0.0])
    v_b = np.array([1.0, 0.0, 0.0, 0.0])
    analyzer = _make_analyzer_with_fake_embed({"identical-a": v_a, "identical-b": v_b})

    score = analyzer._compute_semantic_similarity("identical-a", "identical-b")

    # Identical vectors → cosine 1.0 → mapped to 100 via (cos+1)*50.
    assert 99.5 <= score <= 100.5, f"identical-vector score should be ~100, got {score}"


def test_compute_semantic_similarity_orthogonal_returns_about_50():
    """Orthogonal vectors should map to 50 (mid-scale), NOT 0 — the
    pre-fix mapping (* 100) silently treated negative cosines as
    negative numbers downstream. The new (cos+1)*50 mapping gives
    operators a meaningful 0-100 range."""
    v_a = np.array([1.0, 0.0, 0.0, 0.0])
    v_b = np.array([0.0, 1.0, 0.0, 0.0])
    analyzer = _make_analyzer_with_fake_embed({"a": v_a, "b": v_b})

    score = analyzer._compute_semantic_similarity("a", "b")

    assert 49.5 <= score <= 50.5, f"orthogonal score should be ~50, got {score}"


def test_compute_semantic_similarity_unrelated_texts_give_low_score():
    """The codex finding: pre-fix, the .encode() call raised
    AttributeError, fell into the 85.0 default, and unrelated texts
    came back at 85. Now they actually score on the cosine."""
    v_a = np.array([1.0, 0.0, 0.0, 0.0])
    v_b = np.array([-1.0, 0.0, 0.0, 0.0])  # opposite direction
    analyzer = _make_analyzer_with_fake_embed({"alpha": v_a, "beta": v_b})

    score = analyzer._compute_semantic_similarity("alpha", "beta")

    # Opposite vectors → cosine -1 → mapped to 0.
    assert 0 <= score <= 0.5, f"opposite-direction score should be ~0, got {score}"


def test_compute_semantic_similarity_signals_unavailable_on_failure():
    """A failure in fastembed must return -1.0 (the unavailable
    sentinel), NOT 85.0 (the pre-fix default that the analyzer's
    weighted-average treated as a high score)."""

    class _BrokenEmbed:
        def embed(self, texts):
            raise RuntimeError("synthetic fastembed failure")

    analyzer = QualityAnalyzer.__new__(QualityAnalyzer)
    analyzer.enable_semantic_analysis = True
    analyzer.semantic_available = True
    analyzer.embedding_backend = "fastembed"
    analyzer.embedding_model = _BrokenEmbed()

    score = analyzer._compute_semantic_similarity("a", "b")

    assert score == -1.0, (
        "compute failure must return the -1.0 unavailable sentinel, "
        f"not a default that downstream weighted-average treats as a "
        f"score. got: {score}"
    )


def test_analyze_caps_rating_when_no_signal_at_all():
    """Codex round-2 audit (2026-05-01) finding: lowercase unrelated
    single-sentence inputs have NO entities (zero capitalized words >
    3 chars) AND near-identical structure-similarity. Pre-fix the
    weighted average without semantic could land at quality_rating=100
    with content_preserved=None — operators reading "100" might
    approve a compression that lost everything important.

    Post-fix: when semantic is unavailable AND entities are also
    no-signal, the heuristic-only path caps at 70 ("unsure,
    neutral") so it can't auto-approve high-trust task types like
    security_review (95) or architecture_design (90).
    """
    analyzer = QualityAnalyzer(enable_semantic_analysis=False)
    assert analyzer.semantic_available is False

    # Lowercase, no capitalized "entities", short identical-shape
    # prose. Pre-fix this would have hit quality_rating=100.
    original = "the quick brown fox jumps over the lazy dog"
    compressed = "i prefer pasta with a side of green olives"

    manifest = asyncio.run(
        analyzer.analyze(
            original=original,
            compressed=compressed,
            task_type="security_review",  # requires 95
            method="apollo",
        )
    )

    assert manifest.quality_rating <= 70, (
        "no-signal heuristic-only path must cap at 70 to prevent "
        f"auto-approval of high-trust tasks. got {manifest.quality_rating}"
    )
    assert manifest.quality_summary["content_preserved"] is None


def test_analyze_drops_semantic_weight_when_embeddings_missing():
    """The codex round-1 finding: when no embeddings are available,
    the analyzer pre-fix used semantic=100.0 and contributed 0.4
    weight. Unrelated texts could land at 100% quality. Post-fix the
    semantic component is dropped from the rating; entity + structure
    components carry the full 0.5/0.5 weight.
    """
    # Heuristic-only path — no fastembed.
    analyzer = QualityAnalyzer(enable_semantic_analysis=False)
    assert analyzer.semantic_available is False

    # Two texts with NO shared entities and very different structure.
    original = "Wireless networking guide for router firmware updates."
    compressed = "Cooking pasta requires boiling water and salt."

    manifest = asyncio.run(
        analyzer.analyze(
            original=original,
            compressed=compressed,
            task_type="general",
            method="apollo",
        )
    )

    assert manifest.quality_rating < 80, (
        "unrelated-text quality rating must NOT be 100. "
        f"got {manifest.quality_rating}. semantic-as-100 default would "
        "have masked the entity + structure mismatch."
    )
    assert manifest.quality_summary["content_preserved"] is None, (
        "content_preserved must be None (not a number) when semantic "
        f"analysis is unavailable. got: {manifest.quality_summary['content_preserved']}"
    )


def test_analyze_includes_semantic_when_fastembed_available():
    """Sanity: when fastembed IS available and produces a real score,
    the analyze() pipeline incorporates it into the weighted average
    AND surfaces it as a numeric content_preserved field."""
    v_high = np.array([1.0, 0.0, 0.0, 0.0])
    v_high_too = np.array([0.99, 0.1, 0.0, 0.0])  # high cosine
    mapping = {
        "Wireless networking guide for router firmware updates.": v_high,
        "Wireless guide updates for router firmware.": v_high_too,
    }
    analyzer = _make_analyzer_with_fake_embed(mapping)

    manifest = asyncio.run(
        analyzer.analyze(
            original="Wireless networking guide for router firmware updates.",
            compressed="Wireless guide updates for router firmware.",
            task_type="general",
            method="apollo",
        )
    )

    assert manifest.quality_summary["content_preserved"] is not None
    assert isinstance(manifest.quality_summary["content_preserved"], float)
    assert manifest.quality_summary["content_preserved"] > 50, (
        "high-cosine pair should land in the upper half of the 0-100 "
        f"semantic scale. got: {manifest.quality_summary['content_preserved']}"
    )
