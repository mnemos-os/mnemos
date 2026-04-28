"""PersonSchema detection, encoding, narration."""
from __future__ import annotations

import pytest

from mnemos.domain.compression.apollo_schemas.person import PersonSchema


@pytest.fixture
def schema():
    return PersonSchema()


# ── detect — labeled form ──────────────────────────────────────────────────


def test_detect_labeled_full(schema):
    content = (
        "Name: Alice Chen\n"
        "Role: Senior Engineer\n"
        "Org: Acme Corp\n"
        "Email: alice@acme.com"
    )
    r = schema.detect(content)
    assert r is not None
    assert r.fields["name"] == "Alice Chen"
    assert r.fields["role"] == "Senior Engineer"
    assert r.fields["org"] == "Acme Corp"
    assert r.fields["contact"] == "alice@acme.com"


def test_detect_labeled_with_synonyms(schema):
    """Company/organization/employer are canonicalized to 'org';
    email/phone/handle canonicalized to 'contact'."""
    content = (
        "Name: Bob\n"
        "Title: Director\n"
        "Company: Globex\n"
        "Phone: 555-123-4567"
    )
    r = schema.detect(content)
    assert r is not None
    assert r.fields["role"] == "Director"
    assert r.fields["org"] == "Globex"
    assert r.fields["contact"] == "555-123-4567"


def test_detect_labeled_name_only(schema):
    r = schema.detect("Name: Alice")
    assert r is not None
    assert r.fields["name"] == "Alice"
    # No role/org/contact — confidence stays at baseline 0.55
    assert 0.50 <= r.confidence <= 0.60


# ── detect — loose prose form ─────────────────────────────────────────────


def test_detect_loose_inline(schema):
    content = "Alice Chen, Senior Engineer at Acme, joined the team."
    r = schema.detect(content)
    assert r is not None
    assert r.fields["name"] == "Alice Chen"
    assert "Senior Engineer" in r.fields["role"]
    assert r.fields["org"] == "Acme"


def test_detect_loose_with_email_picked_up(schema):
    content = (
        "Alice Chen, Principal Engineer at Acme Corp. "
        "Reach her at alice@acme.com."
    )
    r = schema.detect(content)
    assert r is not None
    assert r.fields["contact"] == "alice@acme.com"


def test_detect_loose_rejects_non_role_comma(schema):
    """Proper noun + comma + unrelated clause should NOT trigger the
    loose form — would produce a false-positive PersonSchema match."""
    content = "Alice Chen, my old friend, at the park yesterday."
    # No role keyword in the comma-clause, so loose form doesn't match.
    # And no labeled markers, so labeled doesn't match either.
    assert schema.detect(content) is None


def test_detect_confidence_grows_with_fields(schema):
    minimal = schema.detect("Name: Alice")
    full = schema.detect(
        "Name: Alice\nRole: Engineer\nOrg: Acme\nEmail: alice@acme.com"
    )
    assert minimal is not None and full is not None
    assert full.confidence > minimal.confidence


def test_detect_empty_content(schema):
    assert schema.detect("") is None
    assert schema.detect(None) is None  # type: ignore[arg-type]


def test_detect_no_markers_no_match(schema):
    """Content with neither labels nor the "Name, Role at Org" form
    should not match."""
    assert schema.detect("This is some generic prose about databases.") is None


# ── encode ─────────────────────────────────────────────────────────────────


def test_encode_name_only():
    s = PersonSchema()
    match = s.detect("Name: Alice")
    assert match is not None
    assert s.encode(match) == "PERSON:name=Alice"


def test_encode_full():
    s = PersonSchema()
    match = s.detect(
        "Name: Alice Chen\nRole: Senior Engineer\n"
        "Org: Acme Corp\nEmail: alice@acme.com"
    )
    assert match is not None
    encoded = s.encode(match)
    assert encoded.startswith("PERSON:")
    assert "name=Alice Chen" in encoded
    assert "role=Senior Engineer" in encoded
    assert "org=Acme Corp" in encoded
    assert "contact=alice@acme.com" in encoded


def test_encode_deterministic():
    s = PersonSchema()
    match = s.detect(
        "Alice Chen, Senior Engineer at Acme. alice@acme.com"
    )
    assert match is not None
    assert s.encode(match) == s.encode(match)


# ── narrate ────────────────────────────────────────────────────────────────


def test_narrate_full():
    s = PersonSchema()
    out = s.narrate(
        "PERSON:name=Alice Chen|role=Senior Engineer|org=Acme|contact=alice@acme.com"
    )
    assert "Alice Chen" in out
    assert "Senior Engineer" in out
    assert "Acme" in out
    assert "alice@acme.com" in out


def test_narrate_name_only():
    s = PersonSchema()
    out = s.narrate("PERSON:name=Alice")
    assert out.startswith("Alice")


def test_narrate_role_without_org():
    s = PersonSchema()
    out = s.narrate("PERSON:name=Alice|role=Engineer")
    assert "is Engineer" in out


def test_narrate_org_without_role():
    s = PersonSchema()
    out = s.narrate("PERSON:name=Alice|org=Acme")
    assert "works at Acme" in out


def test_narrate_non_person_prefix_passes_through():
    s = PersonSchema()
    assert s.narrate("EVENT:date=2026-04-23") == "EVENT:date=2026-04-23"


def test_narrate_missing_name_passes_through():
    """Without a name field the narrator has nothing to anchor on —
    falls through verbatim rather than raising or inventing."""
    s = PersonSchema()
    out = s.narrate("PERSON:role=Engineer|org=Acme")
    assert "PERSON:" in out  # verbatim fallback
