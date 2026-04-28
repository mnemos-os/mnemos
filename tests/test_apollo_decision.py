"""DecisionSchema detection, encoding, narration."""
from __future__ import annotations

import pytest

from mnemos.domain.compression.apollo_schemas.decision import DecisionSchema


@pytest.fixture
def schema():
    return DecisionSchema()


# ── detect ─────────────────────────────────────────────────────────────────


def test_detect_basic_decision(schema):
    content = "We decided to use postgres because it gives us transactional audit."
    result = schema.detect(content)
    assert result is not None
    assert result.fields["chose"] == "postgres"
    assert "transactional audit" in (result.fields["because"] or "")


def test_detect_with_alternatives(schema):
    content = (
        "Chose postgres because of the transaction guarantees, "
        "over sqlite and mongodb."
    )
    result = schema.detect(content)
    assert result is not None
    assert result.fields["chose"] == "postgres"
    alts = result.fields["alternatives"]
    assert "sqlite" in alts
    assert "mongodb" in alts


def test_detect_requires_marker(schema):
    """Content without any decision marker doesn't match."""
    content = "Postgres is a database. SQLite is also a database."
    assert schema.detect(content) is None


def test_detect_marker_without_choice_rejected(schema):
    """Marker word alone (no parseable choice) doesn't match."""
    content = "We decided."
    assert schema.detect(content) is None


def test_detect_confidence_grows_with_rationale_and_alts(schema):
    minimal = schema.detect("We chose postgres.")
    full = schema.detect(
        "We chose postgres because of the transaction guarantees, "
        "over sqlite and mongodb and redis."
    )
    assert minimal is not None and full is not None
    assert full.confidence > minimal.confidence


def test_detect_caps_alternatives_at_five(schema):
    content = (
        "Chose postgres because of transactions, "
        "over sqlite, mongo, redis, dynamodb, firestore, elasticsearch, neo4j."
    )
    result = schema.detect(content)
    assert result is not None
    assert len(result.fields["alternatives"]) <= 5


def test_detect_empty_content(schema):
    assert schema.detect("") is None
    assert schema.detect(None) is None  # type: ignore[arg-type]


def test_detect_various_marker_verbs(schema):
    """Each marker should be sufficient on its own."""
    for verb_phrase in (
        "We picked postgres because of transactions",
        "Settled on postgres because of transactions",
        "Opted for postgres because of transactions",
        "Went with postgres because of transactions",
    ):
        assert schema.detect(verb_phrase) is not None, (
            f"verb phrase failed: {verb_phrase!r}"
        )


# ── encode ─────────────────────────────────────────────────────────────────


def test_encode_minimal():
    s = DecisionSchema()
    match = s.detect("We chose postgres.")
    assert match is not None
    encoded = s.encode(match)
    assert encoded == "DECISION:chose=postgres"


def test_encode_with_because_and_alternatives():
    s = DecisionSchema()
    match = s.detect(
        "Chose postgres because of transactions, over sqlite and mongodb."
    )
    assert match is not None
    encoded = s.encode(match)
    assert encoded.startswith("DECISION:chose=postgres")
    assert "because=" in encoded
    assert "over=" in encoded
    assert "sqlite" in encoded
    assert "mongodb" in encoded


def test_encode_sanitizes_pipes_in_values():
    """Pipe characters in captured values would break the dense form;
    they must be replaced. Exercise sanitization via the `because`
    field (the `chose` field can't carry pipes under the tightened
    _looks_like_named_choice guard)."""
    s = DecisionSchema()
    match = s.detect(
        "We chose Postgres because audit | compliance requirements."
    )
    assert match is not None
    encoded = s.encode(match)
    # The encoded form should not contain a raw pipe in a value slot.
    # Count pipes — should equal the number of fields minus 1.
    payload = encoded[len("DECISION:"):]
    n_pipes_expected = payload.count("=") - 1
    assert payload.count("|") == n_pipes_expected


def test_encode_deterministic():
    s = DecisionSchema()
    content = "Chose postgres because of transactions, over sqlite."
    e1 = s.encode(s.detect(content))  # type: ignore[arg-type]
    e2 = s.encode(s.detect(content))  # type: ignore[arg-type]
    assert e1 == e2


# ── narrate ────────────────────────────────────────────────────────────────


def test_narrate_basic():
    s = DecisionSchema()
    out = s.narrate("DECISION:chose=postgres|because=transactional audit|over=sqlite,mongo")
    assert "postgres" in out
    assert "transactional audit" in out
    assert "sqlite" in out and "mongo" in out


def test_narrate_minimal_round_trip():
    s = DecisionSchema()
    match = s.detect("We chose postgres.")
    assert match is not None
    out = s.narrate(s.encode(match))
    assert "postgres" in out


def test_narrate_rejects_non_decision_prefix():
    s = DecisionSchema()
    # Non-DECISION prefix falls through verbatim — narration MUST NOT raise.
    out = s.narrate("EVENT:date=2026-04-23|type=release")
    assert out == "EVENT:date=2026-04-23|type=release"


def test_narrate_handles_empty_payload():
    s = DecisionSchema()
    # DECISION: with no fields degrades gracefully.
    out = s.narrate("DECISION:")
    # Either empty or verbatim — just MUST NOT raise.
    assert isinstance(out, str)
