"""CommitSchema — conventional-commit detection."""
from __future__ import annotations

import pytest

from mnemos.domain.compression.apollo_schemas.commit import CommitSchema


@pytest.fixture
def schema():
    return CommitSchema()


# ── detect ──


def test_detect_type_scope_subject(schema):
    r = schema.detect("fix(compression): TemporalRetriever silently returned empty hits")
    assert r is not None
    assert r.fields["type"] == "fix"
    assert r.fields["scope"] == "compression"
    assert r.fields["subject"] == "TemporalRetriever silently returned empty hits"


def test_detect_type_only_no_scope(schema):
    r = schema.detect("feat: add user auth")
    assert r is not None
    assert r.fields["type"] == "feat"
    assert r.fields["subject"] == "add user auth"
    assert "scope" not in r.fields


def test_detect_breaking_marker(schema):
    r = schema.detect("feat(api)!: reshape response body")
    assert r is not None
    assert r.fields.get("breaking") is True


def test_detect_with_body(schema):
    content = (
        "fix(compression): TemporalRetriever silently returned empty hits\n"
        "\n"
        "Two compounding bugs in the interaction between TemporalRetriever\n"
        "and VectorStore, both hidden by a broad except Exception: return []."
    )
    r = schema.detect(content)
    assert r is not None
    assert r.fields.get("body")
    assert "Two compounding bugs" in r.fields["body"][0]


def test_detect_rejects_non_conventional(schema):
    assert schema.detect("Fixed a bug yesterday") is None
    assert schema.detect("Random: prose that has a colon") is None


def test_detect_rejects_unknown_type(schema):
    """'random(scope): ...' isn't a conventional-commit type."""
    assert schema.detect("random(scope): did something") is None


def test_detect_empty(schema):
    assert schema.detect("") is None


# ── encode ──


def test_encode_minimal():
    s = CommitSchema()
    r = s.detect("fix: typo")
    encoded = s.encode(r)
    assert encoded == "COMMIT:type=fix|subject=typo"


def test_encode_with_scope():
    s = CommitSchema()
    r = s.detect("feat(api): add endpoint")
    encoded = s.encode(r)
    assert encoded == "COMMIT:type=feat|scope=api|subject=add endpoint"


def test_encode_with_body():
    s = CommitSchema()
    content = "fix(core): bug X\n\nLong body text here explaining the bug."
    r = s.detect(content)
    encoded = s.encode(r)
    assert encoded.startswith("COMMIT:type=fix|scope=core|subject=bug X|body=[")
    assert "explaining the bug" in encoded


# ── narrate ──


def test_narrate_basic():
    out = CommitSchema().narrate("COMMIT:type=fix|scope=core|subject=bug X")
    assert "fix(core)" in out
    assert "bug X" in out


def test_narrate_with_body():
    encoded = (
        "COMMIT:type=fix|scope=core|subject=bug X|"
        "body=[Line 1 of body.|Line 2 here.]"
    )
    out = CommitSchema().narrate(encoded)
    assert "bug X" in out
    assert "Line 1 of body" in out


def test_narrate_passes_through_non_commit_prefix():
    assert CommitSchema().narrate("CODE:lang=python") == "CODE:lang=python"
