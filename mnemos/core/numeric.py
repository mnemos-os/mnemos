"""Numeric coercion helpers used across the API + DB layers.

Lives in ``mnemos.core`` so the API layer can call into it without
importing from the DB layer (which would violate the layered-
architecture contract: api -> domain, not api -> db).
"""
from __future__ import annotations

from typing import Any


def safe_float(value: Any) -> float:
    """Best-effort float cast with NULL → 0.0 fallback.

    Used at the model-registry seam where Postgres returns
    ``decimal.Decimal`` (or NULL when a column hasn't been backfilled)
    and SQLite returns ``float`` (or ``None``). Both shapes collapse
    to a finite float.

    Unparseable strings also collapse to 0.0 so a defensive caller can
    treat the function as totally safe at boundaries it does not
    control.
    """
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
