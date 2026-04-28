"""EventSchema detection, encoding, narration."""
from __future__ import annotations

import pytest

from mnemos.domain.compression.apollo_schemas.event import EventSchema


@pytest.fixture
def schema():
    return EventSchema()


# ── detect ─────────────────────────────────────────────────────────────────


def test_detect_iso_date_plus_type(schema):
    content = "Incident on 2026-04-23 — stranded-running rows recovered."
    r = schema.detect(content)
    assert r is not None
    assert r.fields["date"] == "2026-04-23"
    assert r.fields["type"] == "incident"


def test_detect_text_date(schema):
    content = "Release on Apr 23, 2026 — v3.2.0 tagged."
    r = schema.detect(content)
    assert r is not None
    assert r.fields["date"] == "2026-04-23"
    assert r.fields["type"] == "release"


def test_detect_day_month_year_order(schema):
    content = "Meeting on 23 April 2026 with the team."
    r = schema.detect(content)
    assert r is not None
    assert r.fields["date"] == "2026-04-23"
    assert r.fields["type"] == "meeting"


def test_detect_slash_date_ymd(schema):
    r = schema.detect("Deployment 2026/04/23 to production.")
    assert r is not None
    assert r.fields["date"] == "2026-04-23"
    assert r.fields["type"] == "deployment"


def test_detect_requires_both_date_and_type(schema):
    # Date present, no type.
    assert schema.detect("Today is 2026-04-23.") is None
    # Type present, no date.
    assert schema.detect("There was an incident with the queue.") is None


def test_detect_type_synonyms_normalize(schema):
    """Input type words map to canonical labels."""
    for marker, canon in (
        ("outage", "incident"),
        ("deploy", "deployment"),
        ("tag", "release"),
        ("sync", "meeting"),
        ("launch", "milestone"),
    ):
        content = f"Had a {marker} on 2026-04-23 yesterday."
        r = schema.detect(content)
        assert r is not None, f"marker {marker!r} failed to match"
        assert r.fields["type"] == canon, (
            f"{marker!r} should normalize to {canon!r}"
        )


def test_detect_picks_up_scope_label(schema):
    content = (
        "Incident on 2026-04-23. scope: compression-worker"
    )
    r = schema.detect(content)
    assert r is not None
    assert r.fields.get("scope") == "compression-worker"


def test_detect_picks_up_description_label(schema):
    content = (
        "Deployment on 2026-04-23. "
        "description: v3.3 APOLLO schemas shipped"
    )
    r = schema.detect(content)
    assert r is not None
    assert "APOLLO schemas" in r.fields.get("desc", "")


def test_detect_caps_description_length(schema):
    long_desc = "x" * 500
    content = f"Incident on 2026-04-23. description: {long_desc}"
    r = schema.detect(content)
    assert r is not None
    assert len(r.fields["desc"]) <= 120


def test_detect_rejects_invalid_calendar_date(schema):
    """'2026-13-45' is not a real date; schema must not match."""
    # Note: regex would match the digits but datetime() constructor
    # rejects, so the schema returns None for the date extraction.
    content = "Incident on 2026-13-45."
    r = schema.detect(content)
    # Either None or matches on type-only (which also means no match
    # because we need both). Assert no match.
    assert r is None


def test_detect_empty_content(schema):
    assert schema.detect("") is None
    assert schema.detect(None) is None  # type: ignore[arg-type]


def test_detect_confidence_growth(schema):
    minimal = schema.detect("Incident on 2026-04-23.")
    rich = schema.detect(
        "Incident on 2026-04-23. scope: worker. "
        "description: queue rows stranded for 10 min."
    )
    assert minimal is not None and rich is not None
    assert rich.confidence > minimal.confidence


# ── encode ─────────────────────────────────────────────────────────────────


def test_encode_minimal():
    s = EventSchema()
    match = s.detect("Release on 2026-04-23.")
    assert match is not None
    assert s.encode(match) == "EVENT:date=2026-04-23|type=release"


def test_encode_with_scope_and_desc():
    s = EventSchema()
    match = s.detect(
        "Deployment on 2026-04-23. scope: api. "
        "description: v3.2.0 tag"
    )
    assert match is not None
    encoded = s.encode(match)
    assert encoded.startswith("EVENT:")
    assert "date=2026-04-23" in encoded
    assert "type=deployment" in encoded
    assert "scope=api" in encoded
    assert "desc=v3.2.0 tag" in encoded


def test_encode_deterministic():
    s = EventSchema()
    match = s.detect("Release on 2026-04-23.")
    assert match is not None
    assert s.encode(match) == s.encode(match)


# ── narrate ────────────────────────────────────────────────────────────────


def test_narrate_basic():
    s = EventSchema()
    out = s.narrate("EVENT:date=2026-04-23|type=release")
    assert "2026-04-23" in out
    assert "Release" in out  # title-cased


def test_narrate_with_scope_and_desc():
    s = EventSchema()
    out = s.narrate(
        "EVENT:date=2026-04-23|type=incident|scope=worker|desc=queue rows stranded"
    )
    assert "2026-04-23" in out
    assert "worker" in out  # scope in parentheses
    assert "queue rows stranded" in out


def test_narrate_non_event_prefix_passes_through():
    s = EventSchema()
    out = s.narrate("PERSON:name=Alice")
    assert out == "PERSON:name=Alice"


def test_narrate_missing_required_fields_passes_through():
    s = EventSchema()
    # Missing type — narrator has nothing to anchor on, returns
    # verbatim rather than inventing.
    out = s.narrate("EVENT:date=2026-04-23")
    assert "EVENT:" in out  # verbatim fallback
