"""Coverage for optional mnemos_hot acceleration in QualityAnalyzer."""
from __future__ import annotations

import importlib
import os
import sys
import types
from typing import Iterable

import numpy as np
import pytest


class _FakeFastEmbed:
    def __init__(self, mapping: dict[str, np.ndarray]):
        self._mapping = mapping
        self._dim = next(iter(mapping.values())).shape[0] if mapping else 4

    def embed(self, texts: Iterable[str]) -> Iterable[np.ndarray]:
        for text in texts:
            yield self._mapping.get(text, np.zeros(self._dim))


def _reload_quality_module(monkeypatch, *, hot_enabled: bool, hot_module=None):
    import mnemos.domain.compression.quality_analyzer as _orig

    if hot_enabled:
        monkeypatch.setenv("MNEMOS_HOT_RS_ENABLED", "1")
    else:
        monkeypatch.delenv("MNEMOS_HOT_RS_ENABLED", raising=False)

    if hot_module is None:
        sys.modules.pop("mnemos_hot", None)
    else:
        sys.modules["mnemos_hot"] = hot_module

    return importlib.reload(_orig)


def _make_analyzer(quality_mod, mapping: dict[str, np.ndarray]):
    analyzer = quality_mod.QualityAnalyzer.__new__(quality_mod.QualityAnalyzer)
    analyzer.enable_semantic_analysis = True
    analyzer.semantic_available = True
    analyzer.embedding_backend = "fastembed"
    analyzer.embedding_model = _FakeFastEmbed(mapping)
    return analyzer


def test_default_branch_uses_numpy_cosine(monkeypatch):
    quality_mod = _reload_quality_module(monkeypatch, hot_enabled=False)
    assert quality_mod._HOT_RS is None

    analyzer = _make_analyzer(
        quality_mod,
        {
            "a": np.array([1.0, 0.0], dtype=np.float32),
            "b": np.array([1.0, 0.0], dtype=np.float32),
        },
    )

    assert analyzer._compute_semantic_similarity("a", "b") == pytest.approx(100.0)


def test_optin_uses_rust_cosine(monkeypatch):
    calls = []

    def cosine(left, right):
        calls.append((left, right))
        return 0.5

    fake = types.SimpleNamespace(__version__="fake-0", cosine=cosine)
    quality_mod = _reload_quality_module(monkeypatch, hot_enabled=True, hot_module=fake)
    analyzer = _make_analyzer(
        quality_mod,
        {
            "a": np.array([1.0, 0.0], dtype=np.float32),
            "b": np.array([0.5, 0.5], dtype=np.float32),
        },
    )

    score = analyzer._compute_semantic_similarity("a", "b")

    assert score == pytest.approx(75.0)
    assert calls == [([1.0, 0.0], [0.5, 0.5])]


def test_optin_falls_back_to_numpy_when_rust_cosine_raises(monkeypatch):
    def cosine(_left, _right):
        raise RuntimeError("synthetic rust failure")

    fake = types.SimpleNamespace(__version__="fake-0", cosine=cosine)
    quality_mod = _reload_quality_module(monkeypatch, hot_enabled=True, hot_module=fake)
    analyzer = _make_analyzer(
        quality_mod,
        {
            "a": np.array([1.0, 0.0], dtype=np.float32),
            "b": np.array([0.0, 1.0], dtype=np.float32),
        },
    )

    score = analyzer._compute_semantic_similarity("a", "b")

    assert score == pytest.approx(50.0)


def test_optin_preserves_zero_norm_unavailable_sentinel(monkeypatch):
    calls = []

    def cosine(left, right):
        calls.append((left, right))
        return 1.0

    fake = types.SimpleNamespace(__version__="fake-0", cosine=cosine)
    quality_mod = _reload_quality_module(monkeypatch, hot_enabled=True, hot_module=fake)
    analyzer = _make_analyzer(
        quality_mod,
        {
            "a": np.array([0.0, 0.0], dtype=np.float32),
            "b": np.array([1.0, 0.0], dtype=np.float32),
        },
    )

    score = analyzer._compute_semantic_similarity("a", "b")

    assert score == -1.0
    assert calls == []


def teardown_module(_):
    import mnemos.domain.compression.quality_analyzer as _orig

    os.environ.pop("MNEMOS_HOT_RS_ENABLED", None)
    sys.modules.pop("mnemos_hot", None)
    importlib.reload(_orig)
