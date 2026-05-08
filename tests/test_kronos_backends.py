from __future__ import annotations

import importlib
import importlib.util
import os
import time

import numpy as np
import pytest


def test_cpu_backend_ewma_known_input():
    from mnemos.domain.kronos.backends import cpu

    values = np.asarray([10, 20, 30], dtype=np.int64)
    result = cpu.ewma(values, alpha=0.5)

    assert result.shape == values.shape
    assert result.dtype == np.dtype(float)
    np.testing.assert_allclose(result, np.asarray([10.0, 15.0, 22.5]))


def test_gpu_backend_imports_and_computes_or_falls_back():
    gpu = importlib.import_module("mnemos.domain.kronos.backends.gpu")

    result = gpu.ewma(np.asarray([10.0, 20.0, 30.0]), alpha=0.5)

    np.testing.assert_allclose(np.asarray(result), np.asarray([10.0, 15.0, 22.5]))
    if importlib.util.find_spec("cupy") is None:
        assert gpu.CUPY_AVAILABLE is False
        assert gpu.BACKEND_NAME == "cpu"


def test_selector_env_var_forces_cpu(monkeypatch):
    from mnemos.domain.kronos.backends.selector import get_backend

    monkeypatch.setenv("MNEMOS_KRONOS_BACKEND", "cpu")

    backend = get_backend()

    assert backend.BACKEND_NAME == "cpu"
    np.testing.assert_allclose(backend.ewma(np.asarray([1.0, 3.0]), alpha=0.25), np.asarray([1.0, 1.5]))


def test_selector_gpu_env_returns_working_backend_without_required_cupy(monkeypatch):
    from mnemos.domain.kronos.backends.selector import get_backend

    monkeypatch.setenv("MNEMOS_KRONOS_BACKEND", "gpu")

    backend = get_backend()
    result = backend.ewma(np.asarray([2.0, 6.0, 10.0]), alpha=0.5)

    np.testing.assert_allclose(np.asarray(result), np.asarray([2.0, 4.0, 7.0]))
    if importlib.util.find_spec("cupy") is None:
        assert backend.BACKEND_NAME == "cpu"


@pytest.mark.skipif(os.getenv("BENCHMARK") != "1", reason="set BENCHMARK=1 to run synthetic KRONOS EWMA timing")
def test_cpu_backend_ewma_benchmark_1m():
    from mnemos.domain.kronos.backends import cpu

    values = np.random.default_rng(0).random(1_000_000).astype(np.float64)
    start = time.perf_counter()
    result = cpu.ewma(values, alpha=0.3)
    elapsed = time.perf_counter() - start

    assert result.shape == values.shape
    assert result.dtype == np.dtype(float)
    assert elapsed < 1.0
