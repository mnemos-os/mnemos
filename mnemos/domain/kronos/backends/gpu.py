"""Optional CuPy compute backend for KRONOS."""

from __future__ import annotations

import logging

import numpy as np

from mnemos.domain.kronos.backends import cpu

logger = logging.getLogger(__name__)

try:
    import cupy as cp
except ImportError:
    logger.warning("CuPy is not available; KRONOS GPU backend is falling back to NumPy")

    CUPY_AVAILABLE = False
    BACKEND_NAME = cpu.BACKEND_NAME
    ewma = cpu.ewma
else:
    CUPY_AVAILABLE = True
    BACKEND_NAME = "gpu"

    def ewma(values: np.ndarray | cp.ndarray, alpha: float = 0.3) -> np.ndarray | cp.ndarray:
        """Exponentially weighted moving average using CuPy when possible."""
        if not 0.0 < alpha <= 1.0:
            raise ValueError("alpha must be in the range (0, 1]")

        input_xp = cp.get_array_module(values)
        return_cupy = input_xp is cp
        arr = cp.asarray(values, dtype=float)
        if arr.size == 0:
            result = arr.astype(float)
            return result if return_cupy else cp.asnumpy(result)

        result = cp.empty_like(arr, dtype=float)
        result[0] = arr[0]
        for idx in range(1, arr.size):
            result[idx] = alpha * arr[idx] + (1.0 - alpha) * result[idx - 1]
        return result if return_cupy else cp.asnumpy(result)
