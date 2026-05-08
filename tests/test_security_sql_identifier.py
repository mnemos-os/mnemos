"""Slice #166: boundary tests for _sql_identifier and _sql_cast.

These helpers in mnemos/core/security.py guard against SQL injection
when callers pass dynamic table/column names through to f-string-based
queries (e.g. assert_owned_context interpolates the table name into
the SELECT statement). The integration paths exercise them indirectly,
but a direct unit test is cheaper to maintain and pins the exact
allowed/rejected patterns. Without these tests, a future "improvement"
that loosens the regex (to support a new naming scheme, for example)
could open an injection door without anyone noticing.
"""
from __future__ import annotations

import pytest

from mnemos.core.security import _sql_cast, _sql_identifier


# ─────────────────────────────────────────────────────────────────────
# _sql_identifier: ^[A-Za-z_][A-Za-z0-9_]*$ per part, dotted allowed
# ─────────────────────────────────────────────────────────────────────


def test_sql_identifier_accepts_simple_names():
    assert _sql_identifier("memories", "table") == "memories"
    assert _sql_identifier("kg_triples", "table") == "kg_triples"
    assert _sql_identifier("_underscored", "column") == "_underscored"


def test_sql_identifier_accepts_dotted_names():
    """Schema-qualified identifiers like `public.memories`."""
    assert _sql_identifier("public.memories", "table") == "public.memories"
    assert _sql_identifier("schema_a.table_b", "table") == "schema_a.table_b"


def test_sql_identifier_accepts_uppercase_and_digits():
    assert _sql_identifier("MyTable", "table") == "MyTable"
    assert _sql_identifier("table42", "table") == "table42"
    assert _sql_identifier("T1.col2", "column") == "T1.col2"


@pytest.mark.parametrize(
    "evil",
    [
        "memories; DROP TABLE memories",
        "memories'; DROP TABLE memories--",
        "memories WHERE 1=1",
        "(SELECT * FROM users)",
        "memories,users",
        "memories UNION SELECT",
        "memories\n--comment",
        "memories\x00",
        "memories OR 1=1",
        "1memories",  # leading digit
        "",  # empty
        ".",  # dot only
        ".memories",  # leading dot
        "memories.",  # trailing dot
        "memories..users",  # double dot
        "memo ries",  # whitespace
        "memo-ries",  # hyphen
        "memo+ries",  # plus
        "memo[ries]",  # brackets
        "メモ",  # non-ASCII
        "memo\\ries",  # backslash
    ],
)
def test_sql_identifier_rejects_injection_attempts(evil):
    with pytest.raises(ValueError) as exc_info:
        _sql_identifier(evil, "table")
    assert "Unsafe SQL table" in str(exc_info.value)


def test_sql_identifier_label_appears_in_error():
    """The label should differentiate table vs column violations."""
    with pytest.raises(ValueError) as exc_info:
        _sql_identifier("bad name", "column")
    assert "column" in str(exc_info.value)


def test_sql_identifier_repr_truncation():
    """The error repr should not leak unbounded attacker-supplied
    bytes — but the current implementation uses repr() which may
    include long strings. This test pins the current shape."""
    huge = "a" * 10000 + "; DROP TABLE memories"
    with pytest.raises(ValueError) as exc_info:
        _sql_identifier(huge, "table")
    # The error message contains repr(huge) which is fine for
    # diagnostics — operators see exactly what tripped the validator.
    assert "Unsafe SQL table" in str(exc_info.value)


# ─────────────────────────────────────────────────────────────────────
# _sql_cast: ^[A-Za-z_][A-Za-z0-9_]*(?:\[\])?$
# ─────────────────────────────────────────────────────────────────────


def test_sql_cast_accepts_simple_types():
    assert _sql_cast("uuid") == "uuid"
    assert _sql_cast("text") == "text"
    assert _sql_cast("integer") == "integer"
    assert _sql_cast("timestamptz") == "timestamptz"


def test_sql_cast_accepts_array_suffix():
    """`uuid[]`, `text[]` are valid Postgres array type casts."""
    assert _sql_cast("uuid[]") == "uuid[]"
    assert _sql_cast("text[]") == "text[]"
    assert _sql_cast("integer[]") == "integer[]"


def test_sql_cast_accepts_uppercase_and_underscores():
    assert _sql_cast("MyType") == "MyType"
    assert _sql_cast("my_type") == "my_type"
    assert _sql_cast("MyType[]") == "MyType[]"


@pytest.mark.parametrize(
    "evil",
    [
        "uuid; DROP TABLE",
        "uuid)",
        "uuid'",
        "(uuid)",
        "uuid,text",
        "uuid OR 1=1",
        "uuid[",  # missing close bracket
        "uuid]",  # missing open bracket
        "uuid[][]",  # double array (we only allow single)
        "1uuid",  # leading digit
        "",
        " uuid",  # leading whitespace
        "uuid ",  # trailing whitespace
        "uuid\n",  # newline
    ],
)
def test_sql_cast_rejects_injection_attempts(evil):
    with pytest.raises(ValueError) as exc_info:
        _sql_cast(evil)
    assert "Unsafe SQL id_cast" in str(exc_info.value)


def test_sql_cast_array_only_allowed_at_end():
    """`[]` is only valid as a single trailing array marker."""
    with pytest.raises(ValueError):
        _sql_cast("uu[]id")
    with pytest.raises(ValueError):
        _sql_cast("[]uuid")


def test_sql_identifier_two_part_with_invalid_segment():
    """Schema.table where the schema part is OK but the table part
    has an injection attempt — must reject the whole thing."""
    with pytest.raises(ValueError):
        _sql_identifier("public.memories; DROP", "table")
    with pytest.raises(ValueError):
        _sql_identifier("bad-schema.memories", "table")
