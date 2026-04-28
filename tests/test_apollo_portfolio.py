"""PortfolioSchema detection, encoding, narration."""

from __future__ import annotations

import pytest

from mnemos.domain.compression.apollo_schemas.portfolio import PortfolioSchema


@pytest.fixture
def schema():
    return PortfolioSchema()


# ── detect ─────────────────────────────────────────────────────────────────

def test_detect_matches_two_positions_plain(schema):
    content = "AAPL 100 shares at 150.25, now 175.50. MSFT 50 at 300 now 310."
    result = schema.detect(content)
    assert result is not None
    assert result.schema_id == "portfolio"
    assert len(result.fields["positions"]) == 2
    p0, p1 = result.fields["positions"]
    assert p0["ticker"] == "AAPL" and p0["shares"] == 100.0
    assert p0["basis"] == 150.25 and p0["current"] == 175.50
    assert p1["ticker"] == "MSFT"


def test_detect_one_position_with_markers_passes(schema):
    content = "My portfolio: GOOG 20 shares at 120 now 135."
    result = schema.detect(content)
    assert result is not None, (
        "Single position + 'portfolio' marker should clear the "
        "with-markers minimum (1)."
    )
    assert len(result.fields["positions"]) == 1


def test_detect_one_position_without_markers_rejected(schema):
    # Prose that happens to mention a single ticker-shaped expression.
    content = "The RFC-100 spec at 150.25 was revised to 175.50 last year."
    # RFC-100 is the only ticker-shaped thing; no portfolio markers.
    result = schema.detect(content)
    assert result is None, (
        "Single-position prose without portfolio markers should NOT "
        "match — too ambiguous."
    )


def test_detect_zero_positions_returns_none(schema):
    assert schema.detect("This is prose with no portfolio content.") is None


def test_detect_skips_english_allcaps(schema):
    # 'AT' and 'THE' match [A-Z]{1,5} and the regex; the allcaps
    # filter should suppress both. Avoid 'shares' in this test prose —
    # it's a portfolio marker that would lower the position threshold
    # and hide the filter effect.
    content = "AT 100 was THE 50 now 60. Then AAPL 10 at 150 now 160."
    result = schema.detect(content)
    # Only AAPL survives the filter; no markers → below min-positions
    # threshold of 2 → no detection.
    assert result is None


def test_detect_deduplicates_same_ticker(schema):
    content = (
        "Holdings include AAPL 100 at 150.25 now 175.50, "
        "plus separately AAPL 50 at 155 now 175. MSFT 30 at 300 now 310."
    )
    result = schema.detect(content)
    assert result is not None
    tickers = [p["ticker"] for p in result.fields["positions"]]
    assert tickers == ["AAPL", "MSFT"], (
        "Same ticker should only appear once — first mention wins."
    )


def test_detect_empty_content_returns_none(schema):
    assert schema.detect("") is None
    assert schema.detect(None) is None  # type: ignore[arg-type]


def test_detect_confidence_grows_with_position_count(schema):
    few = schema.detect("AAPL 100 at 150 now 175. MSFT 50 at 300 now 310.")
    many = schema.detect(
        "AAPL 100 at 150 now 175. MSFT 50 at 300 now 310. "
        "GOOG 20 at 120 now 135. AMZN 10 at 140 now 155. "
        "NVDA 5 at 800 now 900."
    )
    assert few and many
    assert many.confidence > few.confidence


def test_detect_confidence_lifted_by_markers(schema):
    without_markers = schema.detect(
        "AAPL 100 at 150 now 175. MSFT 50 at 300 now 310."
    )
    with_markers = schema.detect(
        "Portfolio holdings: AAPL 100 shares at basis 150 now 175. "
        "MSFT 50 at 300 now 310."
    )
    assert without_markers and with_markers
    assert with_markers.confidence > without_markers.confidence


# ── encode ─────────────────────────────────────────────────────────────────

def test_encode_produces_dense_form(schema):
    match = schema.detect(
        "AAPL 100 shares at 150.25 now 175.50. MSFT 50 at 300 now 310."
    )
    assert match is not None
    encoded = schema.encode(match)
    # Two positions, ';'-separated, with 'unclassified' category default.
    assert encoded == (
        "AAPL:100@150.25/175.50:unclassified;"
        "MSFT:50@300/310:unclassified"
    )


def test_encode_deterministic_same_input(schema):
    """Same source content must produce identical encoded output —
    DAG content-addressing depends on this in S-II."""
    content = "AAPL 100 at 150.25 now 175.50. MSFT 50 at 300 now 310."
    e1 = schema.encode(schema.detect(content))   # type: ignore[arg-type]
    e2 = schema.encode(schema.detect(content))   # type: ignore[arg-type]
    assert e1 == e2


def test_encode_compression_shrinks_token_count(schema):
    """The whole point of APOLLO is fewer tokens than prose. Proves
    monotonic shrinkage on canonical-form multi-position prose —
    S-IC's regex handles canonical forms (ticker N at P now P);
    S-II's LLM fallback will expand coverage to paraphrased prose
    like 'one hundred shares of AAPL at a basis of …'.
    """
    prose = (
        "Portfolio holdings: AAPL 100 at 150.25 now 175.50. "
        "MSFT 50 shares at 300 now 310. GOOG 20 at 120 now 135. "
        "These equities reflect a tech-sector focus."
    )
    match = schema.detect(prose)
    assert match is not None
    encoded = schema.encode(match)
    assert len(encoded.split()) < len(prose.split()) / 2, (
        f"Dense form should be <1/2 the prose token count "
        f"(dense={len(encoded.split())} tokens, "
        f"prose={len(prose.split())} tokens)."
    )


# ── narrate ────────────────────────────────────────────────────────────────

def test_narrate_round_trips_ticker_and_amounts(schema):
    encoded = "AAPL:100@150.25/175.50:tech;MSFT:50@300/310:unclassified"
    narration = schema.narrate(encoded)
    assert "AAPL" in narration
    assert "100 shares" in narration
    assert "150.25" in narration
    assert "175.50" in narration
    assert "MSFT" in narration
    assert "(tech)" in narration   # category surfaces when not 'unclassified'


def test_narrate_reports_direction_up(schema):
    assert "up $" in schema.narrate("AAPL:10@100/110:unclassified")


def test_narrate_reports_direction_down(schema):
    assert "down $" in schema.narrate("AAPL:10@110/100:unclassified")


def test_narrate_malformed_piece_emits_bracketed_marker(schema):
    """Malformed pieces should NOT silently disappear — the reader
    must see the parse failure."""
    out = schema.narrate("VALID:10@100/110:tech;not_a_position;MSFT:5@50/60:tech")
    assert "[unparseable: not_a_position]" in out
    assert "VALID" in out and "MSFT" in out


def test_narrate_empty_string_empty_output(schema):
    assert schema.narrate("") == ""
    assert schema.narrate(";") == ""
