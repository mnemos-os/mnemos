"""Code schema — "file:line + symbol + lang + what".

Dense form:
    CODE:lang=<lang>[|file=<path:line>][|symbols=<sym1,sym2,...>][|desc=<short>]

Example input:
    "Fixed bug in src/compression/apollo.py:205 — the
    _normalize_fallback_output() function returned empty string on
    malformed JSON. Changed it to return None so callers can
    distinguish parse failure from valid-empty output."

Example output:
    CODE:lang=python|file=src/compression/apollo.py:205|symbols=_normalize_fallback_output|desc=return None on parse failure

Detection is strict — requires BOTH a file reference AND at least
one identifiable code element (function signature, inline code via
backticks, or symbol-shaped identifier). False positives would garble
prose that happens to mention a function name.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .base import DetectionResult, Schema

_FILE_LINE_RE = re.compile(
    r"\b(?P<file>(?:\./|/)?(?:[\w.-]+/)*[\w.-]+\.(?P<ext>py|js|ts|tsx|jsx|"
    r"go|rs|java|c|cpp|h|hpp|rb|php|sh|sql|yaml|yml|toml|json|md))"
    r"(?::(?P<line>\d+))?\b"
)

# Code extensions always anchor on their own; doc/config extensions
# alone are weak signal — Codex caught "See report.md:12 for customer
# summary and `class action` risk" firing as code because .md:N + a
# prose-backticked phrase is enough under the old rules.
_DOC_EXTS = frozenset({"md", "json", "yaml", "yml", "toml"})

_LANG_BY_EXT = {
    "py": "python", "js": "javascript", "ts": "typescript",
    "tsx": "typescript", "jsx": "javascript",
    "go": "go", "rs": "rust", "java": "java",
    "c": "c", "cpp": "cpp", "h": "c", "hpp": "cpp",
    "rb": "ruby", "php": "php", "sh": "shell",
    "sql": "sql", "yaml": "yaml", "yml": "yaml",
    "toml": "toml", "json": "json", "md": "markdown",
}

# Backtick-delimited inline code (single or triple).
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

# Code-shape punctuation that separates real inline code from prose
# that just happens to be backticked for emphasis. A backticked run
# with no internal whitespace OR containing any of these characters
# counts as a code signal; prose like ``class action`` does not.
_CODE_PUNCT_RE = re.compile(r"[(){}\[\].=_/\\:<>@$#|*&^~%!+?-]")


def _is_codelike_backtick(s: str) -> bool:
    """Heuristic: does a backticked run look like code, not prose?"""
    if not s:
        return False
    inner = s.strip()
    if not inner:
        return False
    if len(inner) > 80:
        # Long backtick runs are usually prose-emphasis, not code.
        return False
    # No whitespace at all → treat as an identifier-shape token.
    if not any(c.isspace() for c in inner):
        return True
    # Has whitespace → require at least one code-shape char.
    return bool(_CODE_PUNCT_RE.search(inner))

# Function / class signature patterns across common languages.
_SIGNATURE_PATTERNS: List[re.Pattern] = [
    re.compile(r"\bdef\s+(?P<name>\w+)\s*\("),            # Python
    re.compile(r"\bfunction\s+(?P<name>\w+)\s*\("),       # JavaScript
    re.compile(r"\bclass\s+(?P<name>\w+)\b"),             # Python/JS/Java/C++
    re.compile(r"\bfunc\s+(?P<name>\w+)\s*\("),           # Go/Swift
    re.compile(r"\bfn\s+(?P<name>\w+)\s*[(<]"),           # Rust
    re.compile(r"\b(?:public|private|protected)?\s*(?:static\s+)?\w+\s+(?P<name>\w+)\s*\([^)]*\)\s*\{"),  # Java-ish
]

# #181: removed `_SYMBOL_RE` — defined but never referenced. The
# code schema's actual symbol detection runs through
# `_CODE_PUNCT_RE` + the `_FUNC_DEF_PATTERNS` heuristic above; this
# orphaned identifier-shape regex was likely an early draft never
# wired to the extraction path.


class CodeSchema(Schema):
    id = "code"
    version = "0.1"

    def detect(self, content: str) -> Optional[DetectionResult]:
        if not content:
            return None

        file_match = _FILE_LINE_RE.search(content)
        # Only count backticked runs that look like code — not prose
        # emphasis quotes like `class action`.
        inline_code = [
            c for c in _INLINE_CODE_RE.findall(content)
            if _is_codelike_backtick(c)
        ]
        signatures: List[str] = []
        for pat in _SIGNATURE_PATTERNS:
            for m in pat.finditer(content):
                name = m.group("name")
                if name and name not in signatures:
                    signatures.append(name)

        # Strictness: require a CODE-extension file reference OR at
        # least 2 distinct code signals (signatures + codelike inline).
        # A doc/config extension (.md/.json/.yaml/.toml) on its own is
        # not enough — those names appear in prose too often.
        has_file = file_match is not None
        is_code_file = (
            has_file and file_match.group("ext") not in _DOC_EXTS
        )
        code_signals = len(signatures) + len(inline_code)
        if not is_code_file and code_signals < 2:
            return None

        fields: Dict[str, object] = {}

        lang: Optional[str] = None
        if file_match:
            fields["file"] = file_match.group("file")
            if file_match.group("line"):
                fields["file"] = f"{file_match.group('file')}:{file_match.group('line')}"
            lang = _LANG_BY_EXT.get(file_match.group("ext"))
        if lang:
            fields["lang"] = lang
        elif signatures and any(
            pat.pattern.startswith(r"\bdef") for pat in _SIGNATURE_PATTERNS[:1]
            if pat.search(content)
        ):
            fields["lang"] = "python"

        # Collect symbols: signatures first, then inline backticked
        # identifiers. Cap at 5 to keep dense form compact.
        symbols: List[str] = []
        for name in signatures[:3]:
            if name not in symbols:
                symbols.append(name)
        for code in inline_code[:5]:
            code = code.strip()
            if not code or len(code) > 40:
                continue
            if code not in symbols:
                symbols.append(code)
            if len(symbols) >= 5:
                break
        if symbols:
            fields["symbols"] = symbols

        # Desc: first sentence after stripping code markers.
        desc = _extract_description(content)
        if desc:
            fields["desc"] = desc

        # Confidence: baseline 0.55 for file-only, +0.15 per additional
        # evidence type, capped at 0.95.
        confidence = 0.5
        if has_file:
            confidence += 0.2
        if signatures:
            confidence += 0.15
        if inline_code:
            confidence += 0.10
        if "lang" in fields:
            confidence += 0.05
        confidence = min(0.95, confidence)

        return DetectionResult(
            schema_id=self.id,
            schema_version=self.version,
            fields=fields,
            confidence=confidence,
            original_length=len(content),
            notes=(
                f"file={bool(has_file)}, sigs={len(signatures)}, "
                f"inline={len(inline_code)}"
            ),
        )

    def encode(self, match: DetectionResult) -> str:
        parts: List[str] = []
        lang = match.fields.get("lang")
        if lang:
            parts.append(f"lang={_sanitize(lang)}")
        file_ref = match.fields.get("file")
        if file_ref:
            parts.append(f"file={_sanitize(file_ref)}")
        symbols = match.fields.get("symbols") or []
        if symbols:
            parts.append("symbols=" + ",".join(_sanitize(s) for s in symbols))
        desc = match.fields.get("desc")
        if desc:
            parts.append(f"desc={_sanitize(desc)}")
        return "CODE:" + "|".join(parts)

    def narrate(self, encoded: str) -> str:
        if not encoded.startswith("CODE:"):
            return encoded
        fields = _parse_pipe_fields(encoded[len("CODE:"):])
        lang = fields.get("lang")
        file_ref = fields.get("file")
        symbols = fields.get("symbols", "").split(",") if fields.get("symbols") else []
        desc = fields.get("desc")

        parts: List[str] = []
        if file_ref:
            pre = f"{lang.title()} code" if lang else "Code"
            parts.append(f"{pre} in {file_ref}")
        elif lang:
            parts.append(f"{lang.title()} code")
        if symbols:
            sym_str = ", ".join(s.strip() for s in symbols if s.strip())
            parts.append(f"involving {sym_str}")
        sentence = " ".join(parts) + "." if parts else ""
        if desc:
            sentence += " " + desc
            if not sentence.endswith((".", "!", "?")):
                sentence += "."
        return sentence or encoded


def _extract_description(content: str) -> Optional[str]:
    """Strip file refs and inline code, return the first sentence of
    what's left — the human-readable explanation around the code."""
    stripped = _FILE_LINE_RE.sub("", content)
    stripped = _INLINE_CODE_RE.sub("", stripped)
    # Drop fenced code blocks
    stripped = re.sub(r"```[^`]*```", "", stripped, flags=re.DOTALL)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if not stripped:
        return None
    # First sentence / clause up to period, em-dash, or 120 chars.
    first = re.split(r"[.—\n]", stripped, maxsplit=1)[0].strip()
    if not first:
        return None
    return first[:120]


def _sanitize(s: str) -> str:
    if not isinstance(s, str):
        s = str(s)
    return s.replace("|", "/").replace("\n", " ").strip()


def _parse_pipe_fields(payload: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    for seg in payload.split("|"):
        if "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        fields.setdefault(k.strip(), v.strip())
    return fields
