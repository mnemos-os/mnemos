"""Runtime compute backend selector for KRONOS."""

from __future__ import annotations

import importlib
import os
from types import ModuleType


_BACKEND_ENV = "MNEMOS_KRONOS_BACKEND"


def get_backend() -> ModuleType:
    """Return the active KRONOS compute backend module."""
    requested = os.getenv(_BACKEND_ENV, "auto").strip().lower()
    if requested == "cpu":
        return importlib.import_module("mnemos.domain.kronos.backends.cpu")
    if requested == "gpu":
        return importlib.import_module("mnemos.domain.kronos.backends.gpu")
    if requested == "auto":
        try:
            importlib.import_module("cupy")
        except ImportError:
            return importlib.import_module("mnemos.domain.kronos.backends.cpu")
        return importlib.import_module("mnemos.domain.kronos.backends.gpu")
    raise ValueError(f"{_BACKEND_ENV} must be one of: auto, cpu, gpu")
