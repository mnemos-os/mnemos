"""CodeSchema — detection, encode, narrate."""
from __future__ import annotations

import pytest

from mnemos.domain.compression.apollo_schemas.code import CodeSchema


@pytest.fixture
def schema():
    return CodeSchema()


# ── detect ──


def test_detect_python_file_with_function(schema):
    # Realistic memory: identifier marked with backticks (the common
    # convention in dev notes + Markdown). CodeSchema catches symbols
    # inside backticks or function-signature patterns, not bare
    # snake_case words (which would false-positive on prose).
    content = (
        "Fixed bug in src/compression/apollo.py:205 — the "
        "`_normalize_fallback_output` function was returning empty "
        "string on malformed JSON. Changed it to return None."
    )
    r = schema.detect(content)
    assert r is not None
    assert r.fields["file"] == "src/compression/apollo.py:205"
    assert r.fields["lang"] == "python"
    assert "_normalize_fallback_output" in r.fields["symbols"]


def test_detect_requires_file_or_two_signals(schema):
    """Prose that mentions a single backticked identifier doesn't fire."""
    content = "The `foo` function was mentioned in the meeting."
    r = schema.detect(content)
    assert r is None, "single inline code with no file ref shouldn't match"


def test_detect_two_inline_code_signals(schema):
    content = "Call `foo()` then `bar()` to initialize."
    r = schema.detect(content)
    assert r is not None


def test_detect_language_from_extension(schema):
    for ext, lang in (
        ("js", "javascript"), ("ts", "typescript"),
        ("go", "go"), ("rs", "rust"), ("rb", "ruby"),
    ):
        content = f"See file at ./main.{ext} for details."
        r = schema.detect(content)
        assert r is not None
        assert r.fields.get("lang") == lang


def test_detect_empty(schema):
    assert schema.detect("") is None


# ── encode ──


def test_encode_full_fields():
    s = CodeSchema()
    r = s.detect(
        "Fixed src/foo.py:100 — the `parse()` function returns None now."
    )
    assert r is not None
    encoded = s.encode(r)
    assert encoded.startswith("CODE:")
    assert "lang=python" in encoded
    assert "file=src/foo.py:100" in encoded


# ── narrate ──


def test_narrate_basic():
    out = CodeSchema().narrate(
        "CODE:lang=python|file=src/foo.py:100|symbols=parse"
    )
    assert "Python" in out
    assert "src/foo.py:100" in out
    assert "parse" in out


def test_narrate_passes_through_non_code_prefix():
    assert CodeSchema().narrate("COMMIT:type=fix") == "COMMIT:type=fix"
