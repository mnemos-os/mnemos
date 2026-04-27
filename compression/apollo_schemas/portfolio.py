"""Portfolio schema — first APOLLO schema.

Dense form:
    TICKER:shares@basis/current:category[;TICKER:shares@basis/current:category...]

Example:
    AAPL:100@150.25/175.50:tech;MSFT:50@300/310:tech

The shape is adapted from InvestorClaw's consultative-layer data
model. Field shapes only — no code shared. Canonical proof that
`AAPL:100@150.25/175.50:tech` (12 tokens) is equivalent downstream-LLM
context for the ~50-token prose sentence it was derived from lives in
the 2026-04-23 roadmap commit for APOLLO.

Detection heuristic:
  * A single "TICKER N at PRICE now PRICE" line can appear in
    non-portfolio prose (code comments, market chatter). The schema
    therefore requires ≥2 distinct ticker positions OR at least one
    portfolio marker word (portfolio, holdings, basis, shares, ...)
    before claiming a match.
  * Confidence scales with match count and marker density, capped at
    1.0. The contest judge uses this as APOLLO's self-reported
    quality in S-IC; S-II replaces with judge-LLM fidelity scoring.

Category is NOT inferred from free text in S-IC — the encoder stamps
'unclassified'. Category inference (sector, asset-class) is S-II work
with an external ticker-classifier data source.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .base import DetectionResult, Schema


# Single-position regex. Recognizes several prose variants operators
# naturally write. The 'ticker' group is constrained to valid ticker
# lengths (1-5 uppercase letters) to cut false positives on random
# all-caps words; we still defense-filter against English words below.
_POSITION_RE = re.compile(
    r"""
    \b(?P<ticker>[A-Z]{1,5})\b            # ticker like AAPL, MSFT
    [\s,:\-]*                              # separator tokens
    (?P<shares>\d+(?:\.\d+)?)              # share count
    \s*(?:shares?|x|sh\.?)?                # optional unit noun
    [\s,\-]*
    (?:at|@|basis(?:\s+of)?|cost|for)?\s*\$?  # basis preamble
    (?P<basis>\d+(?:\.\d+)?)               # basis price
    [\s,\-]*
    (?:now|current|->|→|/|today)\s*\$?    # current preamble
    (?P<current>\d+(?:\.\d+)?)             # current price
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Filter: tokens that match the ticker regex but are English words
# commonly seen in prose ALL-CAPS. Adding more is cheap — this list
# only suppresses a detection, never invents one.
_COMMON_ALLCAPS_NON_TICKERS = frozenset({
    "AT", "THE", "AND", "OR", "BUT", "FOR", "WITH", "FROM", "TO",
    "IN", "ON", "AS", "BY", "IF", "IS", "IT", "OF", "A", "AN",
    "TODO", "FIXME", "NOTE", "WARN", "INFO", "HTTP", "HTTPS",
    "JSON", "HTML", "SQL", "API", "URL", "UUID", "PR", "CI",
})

# Content words that suggest this is genuinely portfolio prose, not a
# chance regex hit. Presence of these words lifts confidence so the
# schema path wins on clearly-portfolio content.
_PORTFOLIO_MARKERS = (
    "portfolio", "holdings", "shares", "position", "positions",
    "basis", "cost basis", "ticker", "stocks", "equities",
)

# Minimum number of distinct ticker positions required to match
# without marker-word reinforcement. A single "XYZ 100 at 5 now 6"
# line in random prose is too ambiguous.
_MIN_POSITIONS_WITHOUT_MARKERS = 2
_MIN_POSITIONS_WITH_MARKERS = 1


class PortfolioSchema(Schema):
    id = "portfolio"
    version = "0.1"

    def detect(self, content: str) -> Optional[DetectionResult]:
        if not content:
            return None

        lowered = content.lower()
        marker_hits = sum(1 for w in _PORTFOLIO_MARKERS if w in lowered)
        has_markers = marker_hits > 0
        min_positions = (
            _MIN_POSITIONS_WITH_MARKERS if has_markers
            else _MIN_POSITIONS_WITHOUT_MARKERS
        )

        positions: List[Dict] = []
        seen_tickers: set[str] = set()

        for m in _POSITION_RE.finditer(content):
            ticker = m.group("ticker").upper()
            if ticker in _COMMON_ALLCAPS_NON_TICKERS:
                continue
            if ticker in seen_tickers:
                continue  # dedupe same-ticker across mentions
            seen_tickers.add(ticker)
            positions.append({
                "ticker": ticker,
                "shares": float(m.group("shares")),
                "basis": float(m.group("basis")),
                "current": float(m.group("current")),
                "category": None,   # inferred in S-II
            })

        if len(positions) < min_positions:
            return None

        # Confidence lift with count + markers. Count-based baseline
        # grows from 0.55 (1 position) to 0.95 (5+ positions).
        base = 0.5 + 0.1 * min(len(positions), 5)
        conf = min(1.0, base + 0.05 * marker_hits)

        return DetectionResult(
            schema_id=self.id,
            schema_version=self.version,
            fields={"positions": positions},
            confidence=conf,
            original_length=len(content),
            notes=(
                f"{len(positions)} position(s), "
                f"{marker_hits} marker hit(s)"
            ),
        )

    def encode(self, match: DetectionResult) -> str:
        parts: List[str] = []
        for p in match.fields["positions"]:
            ticker = p["ticker"]
            shares = _fmt_num(p["shares"])
            basis = _fmt_num(p["basis"])
            current = _fmt_num(p["current"])
            category = p.get("category") or "unclassified"
            parts.append(f"{ticker}:{shares}@{basis}/{current}:{category}")
        return ";".join(parts)

    def narrate(self, encoded: str) -> str:
        """Rule-based readback. S-II replaces with a small-LLM call.

        Each encoded position expands to one English sentence
        describing the ticker, share count, cost basis, current
        price, and direction of move. Malformed pieces are emitted
        verbatim in [brackets] so the reader sees the shape of the
        parse failure rather than silently losing content.
        """
        sentences: List[str] = []
        for piece in encoded.split(";"):
            piece = piece.strip()
            if not piece:
                continue
            sentences.append(_narrate_position(piece))
        return " ".join(sentences)


def _fmt_num(value: float) -> str:
    """Compact number formatting: integer-valued floats lose the .0,
    non-integer floats keep 2-decimal precision."""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}"


def _narrate_position(piece: str) -> str:
    """Render a single encoded position as one English sentence."""
    try:
        head, category = piece.rsplit(":", 1)
        ticker, rest = head.split(":", 1)
        shares_str, price_part = rest.split("@", 1)
        basis_str, current_str = price_part.split("/", 1)
        basis = float(basis_str)
        current = float(current_str)
        delta = current - basis
        direction = "up" if delta >= 0 else "down"
        category_clause = (
            f" ({category})" if category and category != "unclassified" else ""
        )
        return (
            f"{ticker}{category_clause}: {shares_str} shares at basis "
            f"${basis_str}, currently ${current_str} "
            f"({direction} ${abs(delta):.2f})."
        )
    except (ValueError, IndexError):
        return f"[unparseable: {piece}]"
