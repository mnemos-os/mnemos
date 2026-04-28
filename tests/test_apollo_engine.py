"""APOLLOEngine — contest-facing behavior.

Exercises the engine's supports/compress contract, the schema-loop
fast path, the no-match skip, and a basic metadata round-trip into
the CompressionResult manifest.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from mnemos.domain.compression.apollo import APOLLOEngine
from mnemos.domain.compression.apollo_schemas.base import DetectionResult, Schema
from mnemos.domain.compression.base import (
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)


def _req(content: str) -> CompressionRequest:
    return CompressionRequest(memory_id="m1", content=content, owner_id="default")


# ── supports() ─────────────────────────────────────────────────────────────

def test_supports_true_when_portfolio_schema_matches():
    engine = APOLLOEngine(enable_llm_fallback=False)
    req = _req("Portfolio: AAPL 100 at 150 now 175. MSFT 50 at 300 now 310.")
    assert engine.supports(req) is True


def test_supports_true_when_fallback_enabled_even_without_match():
    """S-II default: LLM fallback on → APOLLO supports everything,
    so the contest records APOLLO's decision on every memory."""
    engine = APOLLOEngine()  # default enable_llm_fallback=True
    req = _req("The fox jumped over the lazy dog. No portfolio content here.")
    assert engine.supports(req) is True


def test_supports_false_when_fallback_disabled_and_no_match():
    """Operator who turns off LLM fallback gets the S-IC behavior:
    APOLLO only supports schema-matching content."""
    engine = APOLLOEngine(enable_llm_fallback=False)
    req = _req("The fox jumped over the lazy dog. No portfolio content here.")
    assert engine.supports(req) is False


def test_supports_false_on_empty_content_without_fallback():
    engine = APOLLOEngine(enable_llm_fallback=False)
    assert engine.supports(_req("")) is False


# ── compress() happy path ──────────────────────────────────────────────────

def test_compress_emits_dense_form_and_manifest():
    # fallback off so the test isolates the schema path without any
    # chance of HTTP being attempted against a fake URL.
    engine = APOLLOEngine(enable_llm_fallback=False)
    req = _req("Portfolio: AAPL 100 at 150.25 now 175.50. MSFT 50 at 300 now 310.")
    result: CompressionResult = asyncio.run(engine.compress(req))

    assert result.error is None
    assert result.succeeded() is True
    assert result.engine_id == "apollo"
    # Version bumped to 0.2 when S-II landed the LLM fallback.
    assert result.engine_version == "0.2"
    assert result.compressed_content is not None
    assert "AAPL:100@150.25/175.50" in result.compressed_content
    assert "MSFT" in result.compressed_content
    assert result.compression_ratio is not None
    assert result.compression_ratio < 1.0   # actually compressed
    assert result.quality_score is not None and 0 < result.quality_score <= 1.0
    assert result.identifier_policy == IdentifierPolicy.STRICT
    assert result.gpu_used is False         # S-IC fast path never touches GPU

    # Manifest carries schema provenance so the audit log is useful.
    assert result.manifest["schema_id"] == "portfolio"
    assert result.manifest["schema_version"] == "0.1"
    assert "schema_confidence" in result.manifest


def test_compress_no_match_no_fallback_returns_clean_error_result():
    """With fallback disabled and no schema matching, compress()
    returns an error result rather than raising."""
    engine = APOLLOEngine(enable_llm_fallback=False)
    req = _req("No portfolio content in this prose.")
    result = asyncio.run(engine.compress(req))

    assert result.error == "no_schema_match"
    assert result.succeeded() is False
    assert result.compressed_content is None
    # v3.3 S-II grew the default schema set to four. Assert the
    # core schemas are present rather than pin the exact list —
    # additional schemas added in later slices should not re-break
    # this test on every extension.
    schemas_tried = result.manifest.get("schemas_tried") or []
    assert "portfolio" in schemas_tried
    assert "decision" in schemas_tried
    assert "person" in schemas_tried
    assert "event" in schemas_tried
    assert result.manifest.get("path") == "schema_only_no_match"


# ── gpu_intent contract ────────────────────────────────────────────────────

def test_engine_declares_gpu_optional():
    """APOLLO is GPU_OPTIONAL: schema fast path is pure Python,
    S-II LLM fallback will use GPU when available."""
    assert APOLLOEngine.gpu_intent == GPUIntent.GPU_OPTIONAL


# ── custom schema registry ─────────────────────────────────────────────────

class _StubSchema(Schema):
    """Always-matches stub for testing the engine's dispatch loop."""
    id = "stub"
    version = "0.1"

    def detect(self, content: str) -> Optional[DetectionResult]:
        if not content:
            return None
        return DetectionResult(
            schema_id=self.id,
            schema_version=self.version,
            fields={"echo": content},
            confidence=0.9,
            original_length=len(content),
        )

    def encode(self, match: DetectionResult) -> str:
        return f"STUB:{match.fields['echo'][:20]}"


def test_engine_respects_custom_schema_registry():
    """Operators must be able to register custom schemas in the
    constructor — part of the 'open to operator-registered schemas'
    contract from the CompressionEngine ABC doc."""
    engine = APOLLOEngine(schemas=[_StubSchema()], enable_llm_fallback=False)
    req = _req("some arbitrary content that wouldn't match portfolio")
    assert engine.supports(req) is True
    result = asyncio.run(engine.compress(req))
    assert result.succeeded()
    assert result.manifest["schema_id"] == "stub"
    assert result.compressed_content is not None
    assert result.compressed_content.startswith("STUB:")


def test_engine_first_match_wins_ordering():
    """When multiple schemas could match, the FIRST one in the
    registry wins — specific-before-general is the rule."""
    portfolio_content = (
        "Portfolio: AAPL 100 at 150 now 175. MSFT 50 at 300 now 310."
    )
    # Register stub FIRST; it always matches.
    from mnemos.domain.compression.apollo_schemas import PortfolioSchema
    engine = APOLLOEngine(
        schemas=[_StubSchema(), PortfolioSchema()],
        enable_llm_fallback=False,
    )
    result = asyncio.run(engine.compress(_req(portfolio_content)))
    assert result.manifest["schema_id"] == "stub", (
        "First-matching schema must win — specific ordering is the "
        "operator's tool for routing content to specific encodings."
    )
