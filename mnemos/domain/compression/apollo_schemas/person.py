"""Person schema — "name / role / org / contact".

Dense form:
    PERSON:name=<n>[|role=<r>][|org=<o>][|contact=<c>]

Example:
    PERSON:name=Alice Chen|role=Senior Engineer|org=Acme|contact=alice@acme.com

Detection is strict by design — false positives in this schema
would garble any memory containing a proper noun plus a role-sounding
word. Requires either an explicit labeled form (``name:`` /
``role:``) OR a clear "Name, Role at Org" sentence pattern with a
contact ID (email / handle / phone) present.

Contact-inference is opt-in: the schema only stamps contact when a
recognizable identifier is present. No speculation on "likely
email" from first+last name.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .base import DetectionResult, Schema

# Labeled-form capture (strict, unambiguous).
#   "Name: Alice Chen"  "Role: Senior Engineer"  etc.
#
# Terminator is newline, semicolon, OR end-of-string. Period and
# comma are allowed inside values (email addresses, "Chen, Ph.D"
# style names, "San Francisco, CA" style orgs) — practical use
# writes each label on its own line anyway.
_LABEL_FIELD_RE = re.compile(
    r"(?P<key>name|role|title|org|organization|company|"
    r"employer|contact|email|phone|handle)\s*:\s*"
    r"(?P<val>[^\n;]+?)(?=\s*(?:[\n;]|$))",
    re.IGNORECASE,
)

# Loose-form capture (inline prose).
# "Alice Chen, Senior Engineer at Acme"
_LOOSE_PERSON_RE = re.compile(
    # Proper-noun name: two+ capitalized words (optional middle), optional comma.
    r"(?P<name>[A-Z][a-zA-Z\-']+(?:\s+[A-Z][a-zA-Z\-']+){1,3})"
    r"\s*,\s*"
    # Role phrase: lowercase or titlecase words, 1-5 words.
    r"(?P<role>[A-Za-z][\w\s\-]{2,40}?)"
    r"\s+at\s+"
    # Org name: capitalized.
    r"(?P<org>[A-Z][\w\-.&\s]{1,40}?)"
    r"(?=\s*[.,;\n]|\s+\()"
)

# Contact identifiers (email / handle / phone).
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_HANDLE_RE = re.compile(r"(?:^|\s)@([A-Za-z0-9_\-]{2,40})\b")
_PHONE_RE = re.compile(r"\b(?:\+?1[\s\-.])?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b")

# A plausible person-name value: one or more capitalized tokens,
# allowing hyphens/apostrophes/periods (initials). Rejects:
#   - lowercased strings
#   - runs with numerics or checklist filler ("item 1")
#   - strings with trailing punctuation beyond a final period
# Sentinel values ("TBD", "placeholder") are rejected separately.
_PERSON_NAME_RE = re.compile(
    r"^[A-Z][a-zA-Z\-'.]*"              # First capitalized token
    r"(?:\s+[A-Z][a-zA-Z\-'.]*){0,3}"   # 0-3 additional capitalized tokens
    r"$"
)

# Non-name values to reject even when they happen to be capitalized.
_NAME_SENTINELS = frozenset({
    "tbd", "n/a", "na", "none", "unknown", "placeholder",
    "everyone", "someone", "anyone", "nobody", "staff", "team",
    "anonymous", "redacted", "see above", "see below",
})

# Role/title keywords — used to validate loose-form role captures.
_ROLE_WORDS = (
    "engineer", "developer", "architect", "manager", "director",
    "lead", "head", "chief", "officer", "founder", "vp", "ceo",
    "cto", "cfo", "coo", "president", "analyst", "designer",
    "scientist", "researcher", "consultant", "admin", "specialist",
    "coordinator", "writer", "editor", "producer", "owner",
)


class PersonSchema(Schema):
    id = "person"
    version = "0.1"

    def detect(self, content: str) -> Optional[DetectionResult]:
        if not content:
            return None

        # Try labeled form first — strictest signal.
        # Codex re-review: the old contact-fallback path let strings
        # like "Name: Q4 Escalation Owner" fire because any
        # non-sentinel Name + any contact anchor was accepted. That's
        # a role/checklist record, not a person. The name-shape check
        # is now authoritative — no contact-based escape hatch.
        labeled_fields = _extract_labeled(content)
        name_val = labeled_fields.get("name")
        if name_val and _looks_like_person_name(name_val):
            return self._build_result(labeled_fields, content, source="labeled")

        # Fall through to loose inline form.
        loose_match = _LOOSE_PERSON_RE.search(content)
        if loose_match is not None:
            role = loose_match.group("role").strip().lower()
            # Require a role-like word to avoid matching "Alice Chen,
            # my friend at the park".
            if any(w in role for w in _ROLE_WORDS):
                fields = {
                    "name": loose_match.group("name").strip(),
                    "role": loose_match.group("role").strip(),
                    "org": loose_match.group("org").strip(),
                }
                # Pick up any contact identifier if present in the
                # surrounding content.
                contact = _first_contact(content)
                if contact:
                    fields["contact"] = contact
                return self._build_result(fields, content, source="loose")

        return None

    def _build_result(
        self, fields: Dict[str, str], content: str, *, source: str,
    ) -> DetectionResult:
        # Normalize synonyms to canonical keys.
        canon: Dict[str, str] = {}
        for k, v in fields.items():
            if k in ("organization", "company", "employer"):
                canon.setdefault("org", v)
            elif k in ("title",):
                canon.setdefault("role", v)
            elif k in ("email", "phone", "handle"):
                canon.setdefault("contact", v)
            else:
                canon.setdefault(k, v)

        # Confidence scales with field count. Baseline 0.55 (name
        # only); full (name + role + org + contact) hits 0.95.
        confidence = 0.55
        for key in ("role", "org", "contact"):
            if canon.get(key):
                confidence += 0.13
        confidence = min(1.0, confidence)

        return DetectionResult(
            schema_id=self.id,
            schema_version=self.version,
            fields=canon,
            confidence=confidence,
            original_length=len(content),
            notes=(
                f"source={source}, "
                f"fields={sorted(k for k, v in canon.items() if v)}"
            ),
        )

    def encode(self, match: DetectionResult) -> str:
        parts: List[str] = [f"name={_sanitize(match.fields['name'])}"]
        for key in ("role", "org", "contact"):
            v = match.fields.get(key)
            if v:
                parts.append(f"{key}={_sanitize(v)}")
        return "PERSON:" + "|".join(parts)

    def narrate(self, encoded: str) -> str:
        if not encoded.startswith("PERSON:"):
            return encoded
        fields = _parse_pipe_fields(encoded[len("PERSON:"):])
        name = fields.get("name", "")
        role = fields.get("role")
        org = fields.get("org")
        contact = fields.get("contact")

        if not name:
            return encoded

        parts: List[str] = [name]
        if role and org:
            parts.append(f"is {role} at {org}")
        elif role:
            parts.append(f"is {role}")
        elif org:
            parts.append(f"works at {org}")

        sentence = " ".join(parts) + "."
        if contact:
            sentence += f" Contact: {contact}."
        return sentence


def _looks_like_person_name(s: str) -> bool:
    """Tight test for plausible person-name values.

    Codex re-review caught that ``Q4 Escalation Owner`` passes the raw
    capitalization regex but is a role label, not a person. Reject
    names that (a) contain any digit, or (b) contain a role/title
    keyword — those are role descriptions that belong in ``role:``,
    not ``name:``.
    """
    if not s:
        return False
    stripped = s.strip().strip(".,;:")
    if not stripped:
        return False
    if stripped.lower() in _NAME_SENTINELS:
        return False
    if not _PERSON_NAME_RE.match(stripped):
        return False
    # Digits in a name are vanishingly rare (Q4, v2, 2FA) and far
    # more often signal a role/ticket/id misfiled under name:.
    if any(c.isdigit() for c in stripped):
        return False
    # Role keywords in the name value mean this is a role label, not
    # a person. Check on a word-boundary, case-insensitive.
    lowered = stripped.lower()
    for word in _ROLE_WORDS:
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return False
    return True


def _extract_labeled(content: str) -> Dict[str, str]:
    """Pull out every ``key: value`` label we recognize. Only runs on
    content that carries at least one label marker — cheap."""
    markers = ("name:", "role:", "title:", "org:", "organization:",
               "company:", "email:", "phone:", "contact:", "handle:")
    lowered = content.lower()
    if not any(m in lowered for m in markers):
        return {}
    out: Dict[str, str] = {}
    for m in _LABEL_FIELD_RE.finditer(content):
        key = m.group("key").strip().lower()
        val = m.group("val").strip()
        if val and key not in out:
            out[key] = val
    return out


def _first_contact(content: str) -> Optional[str]:
    """Return the first recognizable contact identifier, or None."""
    m = _EMAIL_RE.search(content)
    if m:
        return m.group(0)
    m = _HANDLE_RE.search(content)
    if m:
        return "@" + m.group(1)
    m = _PHONE_RE.search(content)
    if m:
        return m.group(0)
    return None


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
