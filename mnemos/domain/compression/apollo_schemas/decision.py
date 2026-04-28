"""Decision schema — "decided X because Y, alternatives considered Z".

Dense form:
    DECISION:chose=<choice>[|because=<rationale>][|over=<alt1,alt2,...>]

Example:
    DECISION:chose=postgres|because=transactional audit chain|over=sqlite,mongo

Detection is strict by design — schema fires only on content that
explicitly signals a decision (marker word required) AND produces a
clean capture of the choice. Ambiguous content passes through to
APOLLO's LLM fallback instead of landing a garbled dense form.

Rationale: false positives in dense form are worse than false
negatives. A bad schema match produces a dense encoding that misses
the meaning; a fallback to LLM extraction captures it properly.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .base import DetectionResult, Schema

# Marker words that must appear before this schema even tries to match.
_DECISION_MARKERS = (
    "decided",
    "chose",
    "picked",
    "selected",
    "went with",
    "settled on",
    "opted for",
    "elected to",
    "rationale",
    "alternatives considered",
)

# Capture the choice: "<marker> (to|to use) <choice>" up to a stop word.
# Stop tokens fall into two groups:
#   - Content markers (need a whitespace prefix, like "\s+because")
#   - Punctuation + end-of-string (no prefix; "postgres." terminates on .)
_CHOSE_RE = re.compile(
    r"\b(?:decided|chose|picked|selected|went\s+with|settled\s+on|"
    r"opted\s+for|elected\s+to)\b"
    r"(?:\s+to(?:\s+use)?)?\s+"
    r"(?P<chose>[A-Za-z][\w\s\-./|]*?)"
    r"(?=\s+(?:because|since|due\s+to|given|over|instead|rather|"
    r"for\s+the|as\s+(?:it|they|the))|\s*[.,;!?]|\s*$)",
    re.IGNORECASE,
)

# Capture the rationale: "<marker> <rationale>" up to a stop word.
_BECAUSE_RE = re.compile(
    r"\b(?:because|since|due\s+to|given|rationale\s*:)\b\s*"
    r"(?P<because>[A-Za-z][^.;]*?)"
    r"(?=\s*(?:\.|;|over|alternatives|\s+instead|$))",
    re.IGNORECASE,
)

# Capture alternatives: "over X, Y" or "alternatives (considered/were): X, Y".
_ALTERNATIVES_RE = re.compile(
    r"\b(?:over|alternatives(?:\s+considered|\s+were)?)\b\s*:?\s*"
    r"(?P<alts>[A-Za-z0-9][^.;]*)",
    re.IGNORECASE,
)


class DecisionSchema(Schema):
    id = "decision"
    version = "0.1"

    def detect(self, content: str) -> Optional[DetectionResult]:
        if not content:
            return None

        lowered = content.lower()
        marker_hits = [m for m in _DECISION_MARKERS if m in lowered]
        if not marker_hits:
            return None

        chose_match = _CHOSE_RE.search(content)
        if chose_match is None:
            return None
        chose = _clean_capture(chose_match.group("chose"))
        if not chose:
            return None

        because_match = _BECAUSE_RE.search(content)
        because = (
            _clean_capture(because_match.group("because"))
            if because_match else None
        )

        # Codex caught:
        #   "We selected participants for the study because attrition
        #    was high"
        # fires as a decision with chose=participants. It's describing
        # a research-methodology step, not capturing a technical
        # decision in the conventional sense. Require both:
        #   (a) the chose value is a noun phrase that could plausibly
        #       be a named technology/tool/approach (starts with an
        #       uppercase letter, or is a short lowercase slug, or is
        #       a quoted/backticked string), AND
        #   (b) a rationale ("because ...") is present — single-verb
        #       pronoun-shaped choices without rationale are more
        #       often descriptive prose than captured decisions.
        if not _looks_like_named_choice(chose):
            return None

        alternatives: List[str] = []
        alts_match = _ALTERNATIVES_RE.search(content)
        if alts_match:
            alts_raw = alts_match.group("alts")
            # Split on comma or " and " (English lists); drop empties.
            alternatives = [
                _clean_capture(a)
                for a in re.split(r",|\s+and\s+", alts_raw)
                if a.strip()
            ]
            # Cap at 5 to keep the dense form compact.
            alternatives = [a for a in alternatives if a][:5]

        # Confidence scales with marker count + presence of rationale +
        # presence of alternatives. Baseline 0.6 on just the choice; up
        # to 1.0 with both rationale and 3+ alternatives.
        confidence = 0.6
        confidence += 0.03 * min(len(marker_hits), 3)
        if because:
            confidence += 0.15
        if alternatives:
            confidence += 0.05 * min(len(alternatives), 3)
        confidence = min(1.0, confidence)

        return DetectionResult(
            schema_id=self.id,
            schema_version=self.version,
            fields={
                "chose": chose,
                "because": because,
                "alternatives": alternatives,
            },
            confidence=confidence,
            original_length=len(content),
            notes=(
                f"chose={chose!r}, "
                f"because={bool(because)}, "
                f"{len(alternatives)} alternative(s)"
            ),
        )

    def encode(self, match: DetectionResult) -> str:
        parts: List[str] = [f"chose={_sanitize(match.fields['chose'])}"]
        if match.fields.get("because"):
            parts.append(f"because={_sanitize(match.fields['because'])}")
        alts = match.fields.get("alternatives") or []
        if alts:
            parts.append("over=" + ",".join(_sanitize(a) for a in alts))
        return "DECISION:" + "|".join(parts)

    def narrate(self, encoded: str) -> str:
        """Rule-based readback. S-III swaps in a small-LLM call."""
        if not encoded.startswith("DECISION:"):
            return encoded
        fields = _parse_pipe_fields(encoded[len("DECISION:"):])
        chose = fields.get("chose", "")
        because = fields.get("because")
        alts = fields.get("over")

        sentences: List[str] = []
        if chose:
            sentence = f"Chose {chose}"
            if because:
                sentence += f" because {because}"
            sentence += "."
            sentences.append(sentence)
        if alts:
            # Normalize comma-separated list to a readable form.
            parts = [a.strip() for a in alts.split(",") if a.strip()]
            if parts:
                sentences.append(f"Alternatives considered: {', '.join(parts)}.")
        return " ".join(sentences) if sentences else encoded


# A "named choice" is something plausibly referring to a technology,
# tool, library, or approach — not prose pronouns/common-noun phrases.
# Three accepting shapes:
#   (1) starts with an uppercase letter (Postgres, React, OAuth2)
#   (2) is a short lowercase slug (postgres, redis-cluster, k8s)
#   (3) is quoted or backticked ("foo bar", `baz`)
_STRICT_NAMED_RE = re.compile(
    r"""^(?:
        [A-Z][\w\-./]*(?:\s+[A-Z0-9][\w\-./]*)*   |   # Capitalized token(s)
        [a-z][\w\-./]{0,30}                       |   # short lowercase slug
        ["'`][^"'`]{1,60}["'`]                        # quoted/backticked
    )$""",
    re.VERBOSE,
)

# A looser "looks like a choice" test: reject bare prose pronouns
# and obviously-descriptive common-noun phrases.
_BAD_CHOICE_TOKENS = frozenset({
    "it", "them", "us", "him", "her", "this", "that", "these", "those",
    "participants", "subjects", "users", "people", "everyone", "everything",
    "some", "none", "all", "many", "few", "several",
})


def _looks_like_named_choice(s: str) -> bool:
    """Filter out pronoun-shaped / methodology-prose 'chose' captures.

    Codex caught that an earlier permissive-multi-word fallback let
    `"new process"` / `"our approach"` / `"the rewrite"` through even
    though they are common-noun prose, not a named technology. Require
    the full `_STRICT_NAMED_RE` shape (Capitalized, lowercase slug, or
    quoted/backticked). Anything looser is ambiguous and should fall
    through to Apollo's LLM fallback instead of producing a bad dense
    encoding.
    """
    if not s:
        return False
    stripped = s.strip()
    if not stripped:
        return False
    if stripped.lower() in _BAD_CHOICE_TOKENS:
        return False
    # Only accept if the strict shape fires. Anything else is prose.
    return bool(_STRICT_NAMED_RE.match(stripped))


def _clean_capture(s: str) -> str:
    """Strip surrounding whitespace/punct from a capture group."""
    return s.strip().strip(".,;:").strip()


def _sanitize(s: str) -> str:
    """Strip characters that would break the pipe-delimited dense form."""
    return s.replace("|", "/").replace("\n", " ").strip()


def _parse_pipe_fields(payload: str) -> Dict[str, str]:
    """Parse ``key=v|key=v`` payload into a dict. Preserves the first
    occurrence of each key."""
    fields: Dict[str, str] = {}
    for seg in payload.split("|"):
        if "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        fields.setdefault(k.strip(), v.strip())
    return fields
