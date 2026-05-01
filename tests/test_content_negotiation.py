"""Unit tests for the Accept-header negotiator.

Covers RFC-7231 cases plus MNEMOS-specific defaults:

  * Missing / blank Accept → default JSON.
  * Explicit ``application/json`` or ``*/*`` → default JSON.
  * Recognised non-default types (``text/plain``,
    ``application/x-apollo-dense``) at q=1 win over default.
  * Highest-q wins when multiple types are listed.
  * q=0 explicit refusal is honoured.
  * Malformed q-values fall back to 1.0 (lenient).
  * Unknown media types fall back to default JSON.
"""
from __future__ import annotations

import pytest

from mnemos.api.content_negotiation import negotiate_narrate_format


@pytest.mark.parametrize(
    "accept,expected",
    [
        ("", None),
        ("application/json", None),
        ("*/*", None),
        ("application/*", None),
        ("text/plain", "prose"),
        ("application/x-apollo-dense", "dense"),
        ("Text/Plain", "prose"),  # case-insensitive
        ("APPLICATION/X-APOLLO-DENSE", "dense"),
    ],
)
def test_basic_negotiation(accept, expected):
    assert negotiate_narrate_format(accept) == expected


def test_text_plain_beats_lower_q_json():
    """A higher-q text/plain wins over a lower-q application/json."""
    accept = "application/json;q=0.5, text/plain;q=0.9"
    assert negotiate_narrate_format(accept) == "prose"


def test_json_beats_lower_q_text_plain():
    accept = "text/plain;q=0.5, application/json;q=0.9"
    assert negotiate_narrate_format(accept) is None


def test_dense_at_q_zero_is_refused():
    """An explicit q=0 means 'never give me this representation'."""
    accept = "application/x-apollo-dense;q=0, application/json"
    assert negotiate_narrate_format(accept) is None


def test_text_plain_at_q_zero_is_refused():
    accept = "text/plain;q=0, application/json"
    assert negotiate_narrate_format(accept) is None


def test_first_specified_wins_on_q_tie():
    """When q values are equal, the order in the header determines
    the choice — recognised non-default types listed first should win
    against equally weighted ``*/*``.
    """
    accept = "text/plain, */*"
    assert negotiate_narrate_format(accept) == "prose"

    # Reversed — */* listed first ties with text/plain at q=1, but
    # */* is the default-JSON marker so we prefer JSON when it's
    # listed first.
    accept = "*/*, text/plain"
    assert negotiate_narrate_format(accept) is None


def test_malformed_q_value_falls_back_to_one():
    """If a q-value is unparseable we lean toward acceptable."""
    accept = "text/plain;q=banana"
    assert negotiate_narrate_format(accept) == "prose"


def test_unknown_types_fall_back_to_default():
    """Types we don't recognise behave like default JSON."""
    accept = "application/xml, image/png"
    assert negotiate_narrate_format(accept) is None


def test_extension_params_ignored():
    accept = "text/plain;charset=utf-8"
    assert negotiate_narrate_format(accept) == "prose"


def test_q_clamping_above_one_treated_as_one():
    """RFC violation but lean toward acceptable rather than reject."""
    accept = "text/plain;q=2.5, application/json"
    assert negotiate_narrate_format(accept) == "prose"


def test_q_clamping_negative_treated_as_zero():
    accept = "text/plain;q=-0.5, application/json"
    assert negotiate_narrate_format(accept) is None


def test_blank_segments_skipped():
    accept = ", , text/plain ,"
    assert negotiate_narrate_format(accept) == "prose"
