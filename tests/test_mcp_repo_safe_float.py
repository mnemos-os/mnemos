"""Regression coverage for the NUMERIC NULL → 0.0 collapse helper.

The PG model_registry NUMERIC columns can be NULL on partially-synced
rows; SQLite returns floats. ``safe_float`` is the seam that makes
the recommendation path behaviourally identical across both backends.
Both mnemos/db/mcp_repo and mnemos/api/routes/providers share it.
"""
from __future__ import annotations

import decimal


def test_safe_float_collapses_none_to_zero():
    from mnemos.core.numeric import safe_float
    assert safe_float(None) == 0.0


def test_safe_float_passes_decimal_through_as_float():
    from mnemos.core.numeric import safe_float
    result = safe_float(decimal.Decimal("3.14"))
    assert isinstance(result, float)
    assert abs(result - 3.14) < 1e-9


def test_safe_float_passes_int_through():
    from mnemos.core.numeric import safe_float
    assert safe_float(7) == 7.0


def test_safe_float_passes_float_through():
    from mnemos.core.numeric import safe_float
    assert safe_float(2.5) == 2.5


def test_safe_float_collapses_unparseable_string_to_zero():
    from mnemos.core.numeric import safe_float
    assert safe_float("not a number") == 0.0


def test_safe_float_parses_numeric_string():
    from mnemos.core.numeric import safe_float
    assert safe_float("4.5") == 4.5
