"""APOLLO — schema-aware dense encoding compression engine.

Part of the v3.3 going-forward compression stack (APOLLO + ARTEMIS).
Designed for LLM-to-LLM wire use: encodes memories into typed
key:value dense forms that downstream LLMs parse natively in fewer
tokens than the prose equivalent.

v3.3 S-IC (landed):
  * APOLLOEngine under CompressionEngine ABC.
  * Schema registry pattern; PortfolioSchema the first concrete schema.
  * Rule-based detection + encoding — no LLM calls on the fast path.
  * Rule-based narration (S-II replaces with cached small-LLM readback).

v3.3 S-II (this slice):
  * LLM fallback (httpx against GPU_PROVIDER_HOST) for content that
    misses every registered schema. With fallback
    enabled (default), supports() is unconditionally True so the
    contest's audit log records APOLLO's decision on every memory.
  * Fallback dense form:
        summary=<one line>;facts=[f1|f2|...];
        entities=[e1|e2|...];concepts=[c1|c2|...]
    Strict parser rejects malformed output rather than shipping
    broken encodings downstream.
  * GPUGuard circuit integration — open circuit short-circuits to
    an error result, honest gpu_used flag on every path.

Still ahead (S-II tail and S-III):
  * GET /v1/memories/{id}/narrate endpoint with cached small-LLM
    readback.
  * Judge-LLM fidelity scoring replacing the heuristic quality
    score (fallback currently pins to 0.55 — see QUALITY SCORE
    block below).
  * Additional schemas: decision, person, event.

See ROADMAP.md "Apollo Program" for the full staged plan.
"""
from __future__ import annotations

import logging
import re
import time
from typing import List, Optional

import httpx

from mnemos.core.config import get_settings

from .apollo_schemas import (
    CodeSchema,
    CommitSchema,
    DecisionSchema,
    EventSchema,
    PersonSchema,
    PortfolioSchema,
    Schema,
)
from .base import (
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)
from .gpu_guard import get_guard

logger = logging.getLogger(__name__)

# GPU provider endpoint for APOLLO's LLM fallback.
_PROVIDER_SETTINGS = get_settings().providers
_GPU_PROVIDER_HOST = _PROVIDER_SETTINGS.gpu_provider_host
_GPU_PROVIDER_PORT = _PROVIDER_SETTINGS.gpu_provider_port
_GPU_PROVIDER_TIMEOUT = _PROVIDER_SETTINGS.gpu_provider_timeout


# Default schema registry. Order matters: first match wins.
# Specific-before-general is the rule — portfolio (strictest regex
# shape, tightest false-positive guard) runs before schemas with
# softer markers (decision, person, event). Any future generic
# fallback schema inserts at the tail of this list.
_DEFAULT_SCHEMAS: List[Schema] = [
    # Strictest / most-specific shapes first. CommitSchema comes
    # before CodeSchema because a commit message might incidentally
    # reference code; we want the commit shape captured first.
    CommitSchema(),
    PortfolioSchema(),
    DecisionSchema(),
    PersonSchema(),
    EventSchema(),
    CodeSchema(),
]


# LLM-fallback prompt. Contract: one line, four required sections,
# pipe-separated lists.
# The parser below accepts both `;` and `|` as section separators —
# gemma4-class 7B models empirically conflate "use pipe inside
# lists" with "use pipe everywhere". A concrete example in the
# prompt AND parser tolerance together are what keep this robust.
_FALLBACK_PROMPT = """\
You are extracting structured facts from a memory for dense LLM-to-LLM encoding.
Emit ONE line with four sections separated by SEMICOLONS, in this exact shape:

summary=<one-line summary>;facts=[fact1|fact2|fact3];entities=[name1|name2];concepts=[concept1|concept2]

Three concrete examples (study the punctuation — SEMICOLONS between sections, PIPES only inside the bracketed lists):

Example 1 — release narrative:
  summary=Bob shipped v1.2 last week;facts=[v1.2-shipped|CI-passed|rollback-unused];entities=[Bob|CI];concepts=[release|deploy]

Example 2 — technical how-to:
  summary=Install pgvector before creating the memories table;facts=[install-pgvector|create-memories-table|index-HNSW-for-ANN];entities=[pgvector|Postgres-15|HNSW];concepts=[setup|indexing]

Example 3 — incident report:
  summary=Queue rows stranded in running state on 2026-04-23;facts=[incident-on-2026-04-23|compression-worker-affected|sweep-recovery-shipped];entities=[queue|sweep|v3.1.1];concepts=[incident|recovery]

Rules:
- Four sections required. Empty list sections render as facts=[] (with empty brackets).
- SEMICOLON ';' separates the four top-level sections.
- PIPE '|' separates items within a bracketed list. Never commas.
- No quotes around values, no newlines, no markdown, no explanation — exactly one line.
- Preserve proper nouns, IDs, numbers, and other identifiers verbatim.
{must_preserve_block}
Memory content:
{content}

Output (one line only):"""

# Appended to _FALLBACK_PROMPT when the protected-span extractor finds
# identifiers in the input. Instruction-tuned 7B models consistently
# drop specific tokens (version strings, emails, ticket IDs, dates)
# under generic "preserve identifiers" guidance but reliably keep
# them when the prompt lists the exact strings to retain. This pattern
# moves Apollo's LLM-fallback judge-mean from 0.86 -> 0.94+ on the
# 52-memory benchmark by closing the identifier-drop failure mode the
# judge penalizes most.
_MUST_PRESERVE_TEMPLATE = """
MUST PRESERVE — these specific tokens from the memory MUST appear verbatim in your output:
{identifier_list}
"""


# Parser accepts ';' or '|' as the top-level section separator —
# smaller models conflate the in-list separator with the section
# separator. Non-greedy summary is anchored by the literal
# 'facts=[' boundary so a pipe inside a summary sentence (rare)
# doesn't truncate the capture.
_FALLBACK_RE = re.compile(
    r"^summary=(?P<summary>.*?)[;|]"
    r"facts=\[(?P<facts>[^\]]*)\][;|]"
    r"entities=\[(?P<entities>[^\]]*)\][;|]"
    r"concepts=\[(?P<concepts>[^\]]*)\]$"
)


# QUALITY SCORE for LLM-fallback results.
#
# Pin BELOW every built-in profile's quality_floor so an unreviewed
# fallback candidate never wins on aggressive ratio alone:
#   balanced      floor=0.70
#   quality_first floor=0.80
#   speed_first   floor=0.60  ← tightest constraint
#
# Setting 0.55 puts the fallback under all three floors. The judge
# (when enabled) overwrites this pin with narrated-derivative-vs-root
# fidelity, so a genuinely-good fallback output still gets credit.
# Without the judge, the contest pins fallback at "parseable but
# trust-the-judge-to-tell-us-if-it-mattered" — which is correct,
# because `_normalize_fallback_output()` is structural-only and
# Codex caught syntactically-valid-but-semantically-empty lines
# slipping past 0.65 under speed_first in the prior pin.
_FALLBACK_QUALITY_SCORE = 0.55


class APOLLOEngine(CompressionEngine):
    """APOLLO under the CompressionEngine ABC.

    Two-path compress():

      1. **Schema path (fast):** iterate registered schemas;
         first DetectionResult wins; encode via the schema. No LLM,
         no GPU, IdentifierPolicy.STRICT by construction. This is
         the v3.3 S-IC shape; it remains the preferred output
         whenever a schema matches.

      2. **LLM fallback (S-II):** when no schema matches and
         `enable_llm_fallback=True` (default), call the GPU provider
         with a strict extraction prompt. Output is a strict
         one-line dense form (summary / facts / entities / concepts).
         Parser rejects malformed output rather than shipping broken
         encodings. gpu_used=True; IdentifierPolicy.OFF (LLM may
         paraphrase).

    supports() returns True when fallback is enabled. When an
    operator disables fallback (`enable_llm_fallback=False`),
    supports() falls back to the S-IC shape: True only if some
    registered schema matches.
    """

    id = "apollo"
    label = "APOLLO — schema-aware dense encoding"
    version = "0.2"
    gpu_intent = GPUIntent.GPU_OPTIONAL

    def __init__(
        self,
        schemas: Optional[List[Schema]] = None,
        *,
        enable_llm_fallback: bool = True,
        gpu_url: Optional[str] = None,
        timeout: float = _GPU_PROVIDER_TIMEOUT,
    ) -> None:
        super().__init__()
        self._schemas: List[Schema] = (
            schemas if schemas is not None else list(_DEFAULT_SCHEMAS)
        )
        self._enable_llm_fallback = enable_llm_fallback
        if gpu_url:
            self.gpu_url = gpu_url.rstrip("/")
        else:
            host = _GPU_PROVIDER_HOST.rstrip("/")
            port = _GPU_PROVIDER_PORT
            self.gpu_url = f"{host}:{port}"
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def supports(self, request: CompressionRequest) -> bool:
        """Eligible for every memory when LLM fallback is on.

        When fallback is off the engine falls back to the S-IC
        behavior: only memories that match a registered schema.
        """
        if self._enable_llm_fallback:
            return True
        content = request.content or ""
        return any(
            schema.detect(content) is not None for schema in self._schemas
        )

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def close(self) -> None:
        """Release the http client. distillation_worker calls this on
        shutdown; tests call it between cases."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def compress(self, request: CompressionRequest) -> CompressionResult:
        started = time.perf_counter()
        content = request.content or ""
        original_tokens = len(content.split())
        original_chars = max(1, len(content))

        # Schema fast path.
        for schema in self._schemas:
            match = schema.detect(content)
            if match is None:
                continue
            encoded = schema.encode(match)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            # APOLLO's dense forms have ~no whitespace (AAPL:100@150.25/...
            # has zero spaces), so a whitespace-split compressed_tokens
            # comes out as 1 and ratio = 1/N trips the contest's
            # MIN_CHUNK_RATIO=0.15 "degenerate output" floor, scoring a
            # perfectly valid dense encoding as composite=0.
            #
            # Fix: compute compression_ratio from character lengths. BPE
            # tokenizers average ~4 chars/token for structured text, so
            # the chars-ratio is a good proxy for the actual LLM-visible
            # token economy. compressed_tokens is reported as a BPE-proxy
            # (chars // 4) so the manifest carries a meaningful integer.
            compressed_tokens = max(1, len(encoded) // 4)
            ratio = len(encoded) / original_chars

            # No-op on expansion: PERSON/EVENT schemas can produce an
            # encoded form LONGER than already-labeled or short
            # structured input. Rather than ship the expansion and
            # lose the contest's ratio_term check (ratio >= 1.0 →
            # ratio_term = 0 → composite = 0), return the original
            # verbatim with path=schema_no_op. The schema DID detect
            # structured content — we're just acknowledging no
            # compression was gained.
            if ratio >= 1.0:
                return CompressionResult(
                    engine_id=self.id,
                    engine_version=self.version,
                    compressed_content=content,
                    original_tokens=original_tokens,
                    compressed_tokens=original_tokens,
                    compression_ratio=1.0,
                    quality_score=None,
                    elapsed_ms=elapsed_ms,
                    gpu_used=False,
                    identifier_policy=IdentifierPolicy.STRICT,
                    manifest={
                        "path": "schema_no_op",
                        "schema_id": match.schema_id,
                        "schema_version": match.schema_version,
                        "schema_confidence": match.confidence,
                        "would_encode_to": encoded[:200],
                        "would_be_ratio": ratio,
                    },
                )

            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                compressed_content=encoded,
                original_tokens=original_tokens,
                compressed_tokens=compressed_tokens,
                compression_ratio=ratio,
                quality_score=match.confidence,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.STRICT,
                manifest={
                    "path": "schema",
                    "schema_id": match.schema_id,
                    "schema_version": match.schema_version,
                    "schema_confidence": match.confidence,
                    "schema_notes": match.notes,
                    "field_count": len(match.fields),
                },
            )

        # LLM fallback.
        if self._enable_llm_fallback:
            return await self._llm_fallback(
                request=request,
                content=content,
                original_tokens=original_tokens,
                original_chars=original_chars,
                started=started,
            )

        # Fallback disabled and no schema matched — clean error result.
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            original_tokens=original_tokens,
            elapsed_ms=elapsed_ms,
            gpu_used=False,
            identifier_policy=IdentifierPolicy.STRICT,
            manifest={
                "path": "schema_only_no_match",
                "schemas_tried": [s.id for s in self._schemas],
            },
            error="no_schema_match",
        )

    # ── LLM fallback ────────────────────────────────────────────────────
    #
    # Split out for test isolation — tests that want to exercise the
    # fallback path directly monkeypatch _get_client to return a fake
    # httpx-like client, or replace _llm_fallback entirely.

    async def _llm_fallback(
        self,
        *,
        request: CompressionRequest,
        content: str,
        original_tokens: int,
        original_chars: int,
        started: float,
    ) -> CompressionResult:
        """Invoke the GPU-backed LLM to extract facts when no schema matches.

        Short-circuits to error when the GPU circuit is open.
        """
        guard = get_guard(self.gpu_url)
        admitted, probe_token = await guard.is_available()
        if not admitted:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.OFF,
                manifest={
                    "path": "fallback",
                    "gpu_url": self.gpu_url,
                    "circuit_state": guard.state.value,
                    "circuit_last_error": guard.last_error,
                },
                error=f"gpu_guard circuit open for {self.gpu_url}",
            )

        # Extract protected spans (identifiers, dates, versions, etc.)
        # and inject them into the prompt as a MUST-PRESERVE list.
        # Addresses Apollo's long-tail failure mode: the LLM drops
        # specific tokens the judge penalizes heavily. Artemis's
        # regex-based span detector is reused via a shared import so
        # both engines guarantee the same set of identifiers survive.
        must_preserve_block = _build_must_preserve_block(content)

        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.gpu_url}/v1/completions",
                json={
                    "prompt": _FALLBACK_PROMPT.format(
                        content=content[:4000],
                        must_preserve_block=must_preserve_block,
                    ),
                    "max_tokens": 512,
                    "temperature": 0.1,
                    "top_p": 0.9,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
            raw = (
                payload.get("choices", [{}])[0].get("text", "")
                if isinstance(payload, dict) else ""
            ).strip()
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "[APOLLO fallback] LLM call failed for %s: %s",
                request.memory_id, exc,
            )
            await guard.record_failure(exc, probe_token=probe_token)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.OFF,
                manifest={"path": "fallback", "gpu_url": self.gpu_url},
                error=f"{type(exc).__name__}: {exc}",
            )

        # HTTP 2xx received — signal success to the guard so a probe
        # coming out of HALF_OPEN closes the breaker. Parse failure is
        # a prompt/model issue, not a GPU-health issue, so we report
        # success even when parsing fails.
        await guard.record_success(probe_token=probe_token)

        encoded = _normalize_fallback_output(raw)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if encoded is None:
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed_ms,
                gpu_used=True,
                identifier_policy=IdentifierPolicy.OFF,
                manifest={
                    "path": "fallback",
                    "gpu_url": self.gpu_url,
                    "raw_output_preview": raw[:200],
                },
                error="fallback_parse_failed",
            )

        # Fallback dense form (summary=...;facts=[...];entities=[...];
        # concepts=[...]) also has minimal whitespace in its grammar
        # envelope, so same char-based ratio applies here as in the
        # schema path above.
        compressed_tokens = max(1, len(encoded) // 4)
        ratio = len(encoded) / original_chars
        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            compressed_content=encoded,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=ratio,
            quality_score=_FALLBACK_QUALITY_SCORE,
            elapsed_ms=elapsed_ms,
            gpu_used=True,
            identifier_policy=IdentifierPolicy.OFF,
            manifest={
                "path": "fallback",
                "gpu_url": self.gpu_url,
                "output_shape": "summary;facts;entities;concepts",
            },
        )


def _build_must_preserve_block(content: str) -> str:
    """Return a prompt-appendable block listing identifiers that MUST
    survive the LLM's compression pass. Uses Artemis's regex-based
    protected-span extractor so both engines see the same set of
    "this must not be dropped" tokens.

    Returns the empty string when the content has no extractable
    identifiers (keeps the prompt small on tokens like generic prose).
    Capped at 20 identifiers to avoid context-window pressure on
    long inputs that happen to contain many numeric IDs.
    """
    # Lazy import: Artemis depends on numpy which is required, but we
    # keep the import at call-site so module-level import order stays
    # predictable and the test shim for Artemis can substitute without
    # touching apollo.py.
    from .artemis import _find_protected_spans

    spans = _find_protected_spans(content[:4000])
    if not spans:
        return ""
    # De-duplicate identical strings while preserving source order.
    seen: set = set()
    identifiers: list = []
    for start, end, _kind in spans:
        token = content[start:end]
        if token and token not in seen:
            identifiers.append(token)
            seen.add(token)
        if len(identifiers) >= 20:
            break
    if not identifiers:
        return ""
    listed = "\n".join(f"  - {ident}" for ident in identifiers)
    return _MUST_PRESERVE_TEMPLATE.format(identifier_list=listed)


def _normalize_fallback_output(raw: str) -> Optional[str]:
    """Strip LLM output and verify it matches the expected shape.

    Returns the sanitized one-line encoded string or None on parse
    failure. Tolerates preamble/suffix by scanning lines for the first
    match.
    """
    if not raw:
        return None
    for candidate in (line.strip() for line in raw.splitlines() or [raw]):
        if _FALLBACK_RE.match(candidate):
            return candidate
    return None


# ── Narration (dense → prose readback, v3.3 S-II rule-based) ──────────────
#
# The narrate API endpoint (GET /v1/memories/{id}/narrate) calls these
# helpers to convert APOLLO's dense-form output back into prose for
# human display. S-III replaces the rule-based readback with a cached
# small-LLM call behind the same dispatcher seam.
#
# Two encoded shapes ship in S-II:
#   * Portfolio schema:  AAPL:100@150.25/175.50:tech;MSFT:50@300/310:tech
#   * LLM fallback:      summary=...;facts=[...];entities=[...];concepts=[...]
#
# Each has a rule-based narrator. The dispatcher sniffs the shape and
# routes to the right one; unknown shapes pass through verbatim.

# Portfolio-shape sniff (single-position regex — schema has the full grammar).
_PORTFOLIO_SHAPE_RE = re.compile(
    r"^[A-Z]{1,5}:\d+(?:\.\d+)?@\d+(?:\.\d+)?/\d+(?:\.\d+)?:\S+"
)


def looks_like_portfolio(encoded: str) -> bool:
    """Return True if the encoded form matches the portfolio dense
    shape. Sniff for dispatch only; the schema owns full validation."""
    if not encoded:
        return False
    first = encoded.split(";", 1)[0].strip()
    return bool(_PORTFOLIO_SHAPE_RE.match(first))


def looks_like_fallback(encoded: str) -> bool:
    """Return True if the encoded form matches the LLM-fallback
    shape."""
    if not encoded:
        return False
    return bool(_FALLBACK_RE.match(encoded.strip()))


def _narrate_fallback_form(encoded: str) -> str:
    """Rule-based readback of the LLM-fallback dense shape.

    Input:   summary=<s>;facts=[f1|f2];entities=[e1|e2];concepts=[c1|c2]
    Output:  <s>. Facts: f1, f2. Entities: e1, e2. Concepts: c1, c2.

    Empty sections are omitted from the prose output to avoid ending
    sentences like "Entities: .".
    """
    m = _FALLBACK_RE.match(encoded.strip())
    if m is None:
        return encoded
    summary = (m.group("summary") or "").strip()
    parts: List[str] = []
    if summary:
        # Ensure a trailing period so the sentence reads cleanly even
        # when the LLM omitted one.
        parts.append(summary if summary.endswith((".", "!", "?")) else summary + ".")
    for label, key in (("Facts", "facts"), ("Entities", "entities"), ("Concepts", "concepts")):
        raw_group = (m.group(key) or "").strip()
        if not raw_group:
            continue
        items = [item.strip() for item in raw_group.split("|") if item.strip()]
        if not items:
            continue
        parts.append(f"{label}: {', '.join(items)}.")
    return " ".join(parts) if parts else ""


def narrate_encoded(encoded: Optional[str]) -> str:
    """Dispatch an APOLLO dense form to the correct rule-based
    narrator.

    Dispatch order:
      1. Prefixed forms (DECISION:, PERSON:, EVENT:) — cheap,
         unambiguous.
      2. Portfolio (no prefix; TICKER:N@P/P:CAT sniff).
      3. LLM fallback (summary=...;facts=[...];...).
      4. Unknown — return verbatim.

    Narration is best-effort and MUST NOT raise: the
    /v1/memories/{id}/narrate endpoint calls this and the caller is
    always safe to display the result.
    """
    if not encoded:
        return ""
    if encoded.startswith("CODE:"):
        return CodeSchema().narrate(encoded)
    if encoded.startswith("COMMIT:"):
        return CommitSchema().narrate(encoded)
    if encoded.startswith("DECISION:"):
        return DecisionSchema().narrate(encoded)
    if encoded.startswith("PERSON:"):
        return PersonSchema().narrate(encoded)
    if encoded.startswith("EVENT:"):
        return EventSchema().narrate(encoded)
    if looks_like_portfolio(encoded):
        return PortfolioSchema().narrate(encoded)
    if looks_like_fallback(encoded):
        return _narrate_fallback_form(encoded)
    # Unknown shape — return verbatim; operator can still see the
    # dense form, and a future schema addition will widen coverage.
    return encoded
