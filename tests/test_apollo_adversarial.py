"""Adversarial / false-positive regression tests.

Each case below was caught by a Codex aggressive review of the
Apollo schemas. They're prose that happened to carry shallow surface
features the schemas keyed on. The tests assert that the schemas
now DO NOT fire on these inputs — they fall through to the LLM
fallback (or Artemis) instead of producing a garbled dense form.
"""
from __future__ import annotations

import pytest

from mnemos.domain.compression.apollo_schemas.code import CodeSchema
from mnemos.domain.compression.apollo_schemas.commit import CommitSchema
from mnemos.domain.compression.apollo_schemas.decision import DecisionSchema
from mnemos.domain.compression.apollo_schemas.event import EventSchema
from mnemos.domain.compression.apollo_schemas.person import PersonSchema

# ── CommitSchema: prose-with-a-colon ──


def test_commit_rejects_casual_prose_with_leading_type():
    # Classic Codex find: matches the header regex but ends with prose
    # punctuation + has no body + has modal-verb phrasing.
    assert CommitSchema().detect(
        "fix: we should probably revisit the onboarding doc tone"
    ) is None


def test_commit_rejects_long_subject_without_body():
    # Over the 72-char conventional-commit soft cap and no body
    # paragraph — signals prose, not a commit.
    long_subject = (
        "fix: this is clearly just a long prose sentence that someone "
        "happened to prefix with fix colon without meaning it as a commit"
    )
    assert CommitSchema().detect(long_subject) is None


def test_commit_still_fires_on_real_short_commit():
    # Guardrail: legitimate conventional commits MUST still fire.
    r = CommitSchema().detect("feat(api): add pagination to /users")
    assert r is not None
    assert r.fields["type"] == "feat"


# ── CodeSchema: markdown reference + prose backticks ──


def test_code_rejects_markdown_reference_with_prose_backticks():
    # Codex adversarial: ``report.md:12`` + ``class action`` reads like
    # code under the old "file OR 2 signals" rule but is pure prose.
    assert CodeSchema().detect(
        "See report.md:12 for customer summary and "
        "`class action` risk"
    ) is None


def test_code_rejects_yaml_path_alone_in_prose():
    # .yaml is doc/config ext — alone should not fire.
    assert CodeSchema().detect(
        "The config lives in ops/values.yaml if you need to tweak it."
    ) is None


def test_code_still_fires_on_python_ref_with_signature():
    # Guardrail: real code mention with both file and signature.
    content = (
        "Fixed src/compression/apollo.py:205 — "
        "def _normalize_fallback_output() returned empty string."
    )
    r = CodeSchema().detect(content)
    assert r is not None
    assert r.fields.get("lang") == "python"


# ── DecisionSchema: methodology prose ──


def test_decision_rejects_study_methodology():
    # Codex caught: "selected participants for the study because..."
    # fires as decision(chose=participants) under the naive rule.
    assert DecisionSchema().detect(
        "We selected participants for the study because attrition "
        "was high"
    ) is None


def test_decision_rejects_pronoun_choice():
    # "chose them" is pronoun-shaped — not a captured decision.
    assert DecisionSchema().detect(
        "We chose them for the pilot group."
    ) is None


def test_decision_rejects_multi_word_lowercase_prose():
    # Codex re-review: "new process" is a common-noun phrase, not a
    # named technology/tool/approach. The prior _looks_like_named_choice
    # accepted multi-word lowercase phrases whose first token wasn't
    # blocklisted; this let plain prose like "we chose new process
    # because attrition was high" fire as a decision capture.
    assert DecisionSchema().detect(
        "We chose new process because attrition was high."
    ) is None


def test_decision_still_fires_on_named_tech_choice():
    # Guardrail: a real decision with named choice + rationale.
    r = DecisionSchema().detect(
        "We chose Postgres because transactional audit chain."
    )
    assert r is not None
    assert r.fields["chose"] == "Postgres"


# ── EventSchema: prose that mentions a date + a content word ──


def test_event_rejects_decision_review_prose():
    # Codex caught: "On <date> we reviewed release notes and decision
    # quality" — "decision" + "review" are prose, not event kinds.
    assert EventSchema().detect(
        "On 2026-04-23 we reviewed the release notes and "
        "decision quality."
    ) is None


def test_event_still_fires_on_real_incident():
    # Guardrail: an actual incident on a date still fires.
    r = EventSchema().detect(
        "On 2026-04-23 we had an incident in compression-worker."
    )
    assert r is not None
    assert r.fields["type"] == "incident"
    assert r.fields["date"] == "2026-04-23"


# ── PersonSchema: checklist with Name:/Role:/Org: placeholder values ──


def test_person_rejects_placeholder_checklist():
    # Codex adversarial: any Name:/Role:/Org: line triggers PersonSchema
    # under the naive labeled-form rule. Require a plausibly
    # person-shaped name value.
    assert PersonSchema().detect(
        "Name: TBD\nRole: TBD\nOrg: ACME"
    ) is None


def test_person_still_fires_on_labeled_two_word_name():
    # Guardrail: a real labeled record with a plausible name fires.
    r = PersonSchema().detect(
        "Name: Alice Chen\nRole: Senior Engineer\nOrg: Acme"
    )
    assert r is not None
    assert r.fields["name"] == "Alice Chen"


def test_person_fires_on_initials_surname():
    # "J. Doe" is a plausible person name (initial + surname) and
    # passes the name-shape check without needing the contact fallback.
    r = PersonSchema().detect(
        "Name: J. Doe\nRole: Reviewer\nOrg: Acme\nEmail: j.doe@acme.com"
    )
    assert r is not None


def test_person_rejects_role_title_with_contact_anchor():
    # Codex re-review: "Name: Q4 Escalation Owner" + email fires under
    # the old contact-fallback path because it's non-sentinel + has a
    # contact. But Q4 has a digit AND Owner is a role keyword — this
    # is a role/checklist row, not a person. PersonSchema must reject.
    assert PersonSchema().detect(
        "Name: Q4 Escalation Owner\n"
        "Role: TBD\n"
        "Org: Acme\n"
        "Email: owner@acme.com"
    ) is None


def test_person_rejects_pure_role_in_name_field():
    # Even without a digit, a name field containing a role keyword
    # (Engineer, Manager, Lead, etc.) is a role label, not a person.
    assert PersonSchema().detect(
        "Name: Platform Lead\n"
        "Role: -\n"
        "Org: Acme\n"
        "Email: lead@acme.com"
    ) is None


# ── schema_id tags on any surviving detection ──


@pytest.mark.parametrize(
    "schema_cls, content",
    [
        (CommitSchema, "fix: we should probably revisit onboarding"),
        (CodeSchema, "See report.md:12 for customer summary and `class action` risk"),
        (DecisionSchema, "We selected participants for the study because attrition was high"),
        (EventSchema, "On 2026-04-23 we reviewed the release notes and decision quality."),
        (PersonSchema, "Name: TBD\nRole: TBD\nOrg: ACME"),
    ],
)
def test_all_five_adversarial_cases_return_none(schema_cls, content):
    assert schema_cls().detect(content) is None
