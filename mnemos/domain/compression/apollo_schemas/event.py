"""Event schema — "date / type / scope / description".

Dense form:
    EVENT:date=<iso>|type=<type>[|scope=<scope>][|desc=<description>]

Example:
    EVENT:date=2026-04-23|type=incident|scope=compression-worker|desc=stranded-running rows recovered

Detection requires BOTH (a) a recognizable date in the content AND
(b) a type marker (incident / meeting / release / deployment / etc).
Dates alone or type words alone don't fire — too ambiguous.

Dates are normalized to ISO-8601 (YYYY-MM-DD) on encode so downstream
consumers get a canonical form regardless of how operators wrote
the input (2026-04-23, Apr 23 2026, 23 April 2026, etc.).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .base import DetectionResult, Schema

# Event-type markers. Value is the canonical type label used in
# the encoded form.
_EVENT_TYPES = {
    "incident": "incident",
    "outage": "incident",
    "deployment": "deployment",
    "deploy": "deployment",
    "release": "release",
    "tag": "release",
    "meeting": "meeting",
    "sync": "meeting",
    "call": "meeting",
    "milestone": "milestone",
    "launch": "milestone",
    "publication": "publication",
    "post": "publication",
    "handoff": "handoff",
    "announcement": "announcement",
    # "decision" and "review" deliberately excluded as event types —
    # Codex caught that "On 2026-04-23 we reviewed the release notes
    # and decision quality" would fire EventSchema because those
    # words appeared in the content. They're descriptions of what
    # happened, not event kinds. DecisionSchema already covers
    # formal decision captures; prose mentioning a review should
    # fall through to Artemis rather than mislabel as an event.
}

# Date patterns we understand. Each capture group resolves to a
# datetime; the first match wins.
_DATE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # 2026-04-23 / 2026/04/23
    (re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b"), "ymd"),
    # 04/23/2026 (US)
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b"), "mdy"),
    # 23 April 2026 / Apr 23 2026
    (re.compile(
        r"\b(?P<d1>\d{1,2})?\s*"
        r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
        r"(?:[a-z]*)?"
        r"\s+(?P<d2>\d{1,2})?\s*,?\s*"
        r"(?P<y>20\d{2})\b",
        re.IGNORECASE,
     ), "text"),
]

_MONTH_TO_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _extract_date(content: str) -> Optional[str]:
    """Return ISO-8601 date string (YYYY-MM-DD) or None. First match wins."""
    for pat, fmt in _DATE_PATTERNS:
        m = pat.search(content)
        if not m:
            continue
        try:
            if fmt == "ymd":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif fmt == "mdy":
                mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif fmt == "text":
                mo = _MONTH_TO_NUM.get(m.group("mon").lower()[:4].rstrip("."))
                if mo is None:
                    mo = _MONTH_TO_NUM.get(m.group("mon").lower()[:3])
                if mo is None:
                    continue
                day_str = m.group("d1") or m.group("d2")
                if not day_str:
                    continue
                d = int(day_str)
                y = int(m.group("y"))
            else:
                continue
            # Validate: datetime rejects impossible dates.
            dt = datetime(y, mo, d)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            continue
    return None


# Compound-noun suffixes that mean the event-type word is a MODIFIER,
# not a standalone event claim. e.g. "release notes" = documentation,
# not a release event.
_EVENT_COMPOUND_SUFFIXES = (
    "notes", "candidate", "candidates", "plan", "plans", "schedule",
    "schedules", "timeline", "timelines", "calendar", "history",
    "log", "logs", "archive", "archives", "doc", "docs",
    "summary", "summaries", "quality",
)


def _extract_event_type(content: str) -> Optional[str]:
    """First type-marker hit wins; canonical label returned."""
    lowered = content.lower()
    # Word-boundary scan; longer markers (e.g. "outage") won before
    # shorter ones that could subsume them.
    for marker in sorted(_EVENT_TYPES.keys(), key=len, reverse=True):
        # Codex regression: "release notes" / "incident history" /
        # "deployment plan" are compound-noun descriptions, not event
        # kinds. Require the marker NOT be immediately followed by a
        # descriptor that recasts it as a modifier.
        suffix_alt = "|".join(re.escape(s) for s in _EVENT_COMPOUND_SUFFIXES)
        pattern = rf"\b{re.escape(marker)}\b(?!\s+(?:{suffix_alt})\b)"
        if re.search(pattern, lowered):
            return _EVENT_TYPES[marker]
    return None


# Optional scope / desc extractors.
_SCOPE_LABEL_RE = re.compile(
    r"\bscope\s*:\s*(?P<scope>[^\n;.,]+)", re.IGNORECASE,
)
_DESC_LABEL_RE = re.compile(
    r"\b(?:description|desc|summary)\s*:\s*(?P<desc>[^\n;]+)", re.IGNORECASE,
)


class EventSchema(Schema):
    id = "event"
    version = "0.1"

    def detect(self, content: str) -> Optional[DetectionResult]:
        if not content:
            return None

        date = _extract_date(content)
        if not date:
            return None

        event_type = _extract_event_type(content)
        if not event_type:
            return None

        fields: Dict[str, str] = {"date": date, "type": event_type}

        scope_match = _SCOPE_LABEL_RE.search(content)
        if scope_match:
            fields["scope"] = scope_match.group("scope").strip()

        desc_match = _DESC_LABEL_RE.search(content)
        if desc_match:
            # Cap the description to keep the dense form compact.
            fields["desc"] = desc_match.group("desc").strip()[:120]

        # Confidence: both date + type hit = 0.75 baseline; scope
        # and desc each add 0.1 to max 0.95.
        confidence = 0.75
        if fields.get("scope"):
            confidence += 0.10
        if fields.get("desc"):
            confidence += 0.10
        confidence = min(0.95, confidence)

        return DetectionResult(
            schema_id=self.id,
            schema_version=self.version,
            fields=fields,
            confidence=confidence,
            original_length=len(content),
            notes=(
                f"date={date}, type={event_type}, "
                f"scope={'yes' if fields.get('scope') else 'no'}, "
                f"desc={'yes' if fields.get('desc') else 'no'}"
            ),
        )

    def encode(self, match: DetectionResult) -> str:
        parts: List[str] = [
            f"date={match.fields['date']}",
            f"type={match.fields['type']}",
        ]
        for key in ("scope", "desc"):
            v = match.fields.get(key)
            if v:
                parts.append(f"{key}={_sanitize(v)}")
        return "EVENT:" + "|".join(parts)

    def narrate(self, encoded: str) -> str:
        if not encoded.startswith("EVENT:"):
            return encoded
        fields = _parse_pipe_fields(encoded[len("EVENT:"):])
        date = fields.get("date")
        event_type = fields.get("type")
        scope = fields.get("scope")
        desc = fields.get("desc")

        if not (date and event_type):
            return encoded

        sentence = f"{event_type.title()} on {date}"
        if scope:
            sentence += f" ({scope})"
        sentence += "."
        if desc:
            sentence += f" {desc}" + ("" if desc.endswith((".", "!", "?")) else ".")
        return sentence


def _sanitize(s: str) -> str:
    return s.replace("|", "/").replace("\n", " ").strip()


def _parse_pipe_fields(payload: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for seg in payload.split("|"):
        if "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        fields.setdefault(k.strip(), v.strip())
    return fields
