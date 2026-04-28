"""Commit schema — conventional-commit format.

Dense form:
    COMMIT:type=<type>[|scope=<scope>]|subject=<subject>[|body=<b1|b2|...>]

Example input:
    fix(compression): TemporalRetriever silently returned empty hits

    Two compounding bugs in the interaction between TemporalRetriever
    and VectorStore, both hidden by a broad except Exception: return [].

Example output:
    COMMIT:type=fix|scope=compression|subject=TemporalRetriever silently returned empty hits|body=[Two compounding bugs...]

Detection requires the first non-blank line to match the conventional-
commit shape: ``<type>(<scope>)?: <subject>``. Falls through to prose
fallback on anything else — prose memories that happen to mention a
commit hash aren't misclassified.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .base import DetectionResult, Schema

# Canonical conventional-commit types. Extra types common in real
# repos (hotfix, release, wip) included for tolerance.
_COMMIT_TYPES = frozenset({
    "feat", "fix", "docs", "style", "refactor", "test", "chore",
    "perf", "ci", "build", "revert", "hotfix", "release", "wip",
})

# First-line shape: type(optional-scope): subject
_HEADER_RE = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[^)]{1,40})\))?"
    r"(?P<breaking>!)?"
    r":\s+"
    r"(?P<subject>.+?)$",
    re.MULTILINE,
)

# Body is whatever comes after the first blank line after the header.
_BODY_SPLIT_RE = re.compile(r"\n\s*\n")


class CommitSchema(Schema):
    id = "commit"
    version = "0.1"

    def detect(self, content: str) -> Optional[DetectionResult]:
        if not content:
            return None

        # Find the first non-blank line.
        lines = content.lstrip().split("\n")
        if not lines:
            return None
        header = lines[0]

        m = _HEADER_RE.match(header)
        if m is None:
            return None
        commit_type = m.group("type").lower()
        if commit_type not in _COMMIT_TYPES:
            return None

        # False-positive guard: Codex caught that
        #   "fix: we should probably revisit the onboarding doc tone"
        # matches the header regex even though it's casual prose, not
        # a commit. Real commit subjects have three properties that
        # casual-prose-with-a-colon don't reliably share:
        #   (a) imperative-mood subjects tend to be short (<= 80 chars)
        #   (b) commits without a body are usually trivial; commits
        #       with substantial prose typically have a body paragraph
        #       separated by a blank line
        #   (c) commit headers rarely end with modal-verb phrasings
        #       like "should", "probably", "might" that prose uses
        # Require EITHER a short subject OR a body paragraph to fire.
        # Rejects casual prose that happens to lead with `fix:` / `docs:`
        # / `chore:` while still catching terse commits AND long ones
        # with proper bodies.
        subject = m.group("subject").strip()
        # Subject MUST NOT end with punctuation typical of prose.
        if subject.endswith((".", "!", "?", ":")):
            return None
        # Detect a body: blank line followed by more content after the
        # header line.
        has_body = bool(
            re.search(r"\n\s*\n\S", content, flags=re.DOTALL)
        )
        if len(subject) > 72 and not has_body:
            # Long subject without a body is more likely prose. Real
            # commit conventions target 50-72 chars for the header.
            return None
        # Reject casual-prose cues in the subject itself.
        prose_tells = (
            " should ", " probably ", " might ", " maybe ",
            " we'll ", " i'll ", " we're ", " we are ",
        )
        subj_lower = " " + subject.lower() + " "
        if any(tell in subj_lower for tell in prose_tells):
            return None

        fields: Dict[str, object] = {
            "type": commit_type,
            "subject": subject,
        }
        scope = m.group("scope")
        if scope:
            fields["scope"] = scope.strip()
        if m.group("breaking"):
            fields["breaking"] = True

        # Body: paragraphs after the first blank line, capped at 3
        # paragraphs for the dense form.
        body_start = header.find(header) + len(header)
        remainder = content[body_start:].lstrip("\n")
        body_paragraphs: List[str] = []
        if remainder:
            parts = _BODY_SPLIT_RE.split(remainder)
            # First part after header blank is the body proper; keep up to 3.
            for part in parts[:3]:
                cleaned = " ".join(part.split())
                if not cleaned:
                    continue
                body_paragraphs.append(cleaned[:300])
        if body_paragraphs:
            fields["body"] = body_paragraphs

        confidence = 0.75
        if scope:
            confidence += 0.10
        if body_paragraphs:
            confidence += 0.10
        confidence = min(0.95, confidence)

        return DetectionResult(
            schema_id=self.id,
            schema_version=self.version,
            fields=fields,
            confidence=confidence,
            original_length=len(content),
            notes=(
                f"type={commit_type}, scope={scope or '-'}, "
                f"body_parts={len(body_paragraphs)}"
            ),
        )

    def encode(self, match: DetectionResult) -> str:
        parts: List[str] = [f"type={_sanitize(match.fields['type'])}"]
        if match.fields.get("scope"):
            parts.append(f"scope={_sanitize(match.fields['scope'])}")
        if match.fields.get("breaking"):
            parts.append("breaking=true")
        parts.append(f"subject={_sanitize(match.fields['subject'])}")
        body = match.fields.get("body") or []
        if body:
            rendered = "|".join(_sanitize(b) for b in body)
            parts.append(f"body=[{rendered}]")
        return "COMMIT:" + "|".join(parts)

    def narrate(self, encoded: str) -> str:
        if not encoded.startswith("COMMIT:"):
            return encoded
        fields = _parse_pipe_fields(encoded[len("COMMIT:"):])
        commit_type = fields.get("type", "")
        scope = fields.get("scope")
        subject = fields.get("subject", "")
        breaking = fields.get("breaking") == "true"
        body_raw = fields.get("body", "")

        header_parts: List[str] = []
        if commit_type:
            header = commit_type
            if scope:
                header += f"({scope})"
            if breaking:
                header += "!"
            header_parts.append(header)
        if subject:
            if header_parts:
                header_parts[0] = f"{header_parts[0]}: {subject}"
            else:
                header_parts.append(subject)
        sentence = header_parts[0] if header_parts else encoded

        # Body: strip the outer brackets and split on pipes.
        body_text = body_raw.strip()
        if body_text.startswith("[") and body_text.endswith("]"):
            body_text = body_text[1:-1]
        paragraphs = [p.strip() for p in body_text.split("|") if p.strip()]
        if paragraphs:
            sentence += ". " + " ".join(paragraphs[:3])
        return sentence if sentence.endswith((".", "!", "?")) else sentence + "."


def _sanitize(s) -> str:
    if not isinstance(s, str):
        s = str(s)
    return s.replace("|", "/").replace("\n", " ").strip()


def _parse_pipe_fields(payload: str) -> Dict[str, str]:
    """Pipe-split with awareness of bracketed body field that can
    itself contain pipes."""
    fields: Dict[str, str] = {}
    # Handle body=[...] specially: find the body= marker, extract
    # its bracketed content, then parse the rest normally.
    body_idx = payload.find("|body=[")
    body_val: Optional[str] = None
    if body_idx >= 0:
        close = payload.find("]", body_idx)
        if close > body_idx:
            body_val = payload[body_idx + len("|body=["):close]
            payload = payload[:body_idx] + payload[close + 1:]
    for seg in payload.split("|"):
        if "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        fields.setdefault(k.strip(), v.strip())
    if body_val is not None:
        fields["body"] = body_val
    return fields
