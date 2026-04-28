"""CompressionEngine ABC — contract tests.

Static / in-process checks only (no DB, no GPU). Pins the shape of the
plugin interface that every built-in engine (APOLLO/ARTEMIS) and every
operator-registered plugin must honor:

  * class-level id/label are required; __init__ raises NotImplementedError
    if either is empty (protects against copy-paste subclasses that
    forget to rename themselves)
  * compress() is abstract — ABC cannot be instantiated directly
  * CompressionResult.succeeded() returns True only when error is None
    AND compressed_content + compression_ratio are both populated
  * GPUIntent and IdentifierPolicy enum values are stable (the DB's
    CHECK constraints and the scoring-profile config file both reference
    these string values)
  * Budget-aware constants are unchanged (downstream engines depend on
    the MNEMOS defaults, not OpenClaw's — we pin them here so a drift
    shows up in CI)
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import is_dataclass

import pytest

from mnemos.domain.compression.base import (
    BASE_CHUNK_RATIO,
    MIN_CHUNK_RATIO,
    SAFETY_MARGIN,
    SUMMARIZATION_OVERHEAD_TOKENS,
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)


def test_compression_engine_is_abstract():
    assert inspect.isabstract(CompressionEngine)


def test_request_and_result_are_dataclasses():
    assert is_dataclass(CompressionRequest)
    assert is_dataclass(CompressionResult)


def test_enum_string_values_are_stable():
    # The DB CHECK constraints in migrations_v3_1_compression.sql encode
    # these string values; changing them requires a migration.
    assert GPUIntent.CPU_ONLY.value == "cpu_only"
    assert GPUIntent.GPU_OPTIONAL.value == "gpu_optional"
    assert GPUIntent.GPU_REQUIRED.value == "gpu_required"

    assert IdentifierPolicy.STRICT.value == "strict"
    assert IdentifierPolicy.OFF.value == "off"
    assert IdentifierPolicy.CUSTOM.value == "custom"


def test_budget_constants_pinned():
    assert BASE_CHUNK_RATIO == 0.4
    assert MIN_CHUNK_RATIO == 0.001
    assert SAFETY_MARGIN == 1.2
    assert SUMMARIZATION_OVERHEAD_TOKENS == 4096


def test_subclass_without_id_label_raises():
    class NoIdentity(CompressionEngine):
        async def compress(self, request):
            return CompressionResult(
                engine_id="x", engine_version="1", original_tokens=0
            )

    with pytest.raises(NotImplementedError) as exc:
        NoIdentity()
    msg = str(exc.value)
    assert "id" in msg and "label" in msg


def test_subclass_with_identity_instantiates_and_runs():
    class Echo(CompressionEngine):
        id = "echo"
        label = "Echo Engine"
        version = "0.1"
        gpu_intent = GPUIntent.CPU_ONLY

        async def compress(self, request: CompressionRequest) -> CompressionResult:
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=len(request.content.split()),
                compressed_tokens=len(request.content.split()),
                compressed_content=request.content,
                compression_ratio=1.0,
                quality_score=1.0,
            )

    engine = Echo()
    req = CompressionRequest(memory_id="mem-1", content="hello world")
    res = asyncio.run(engine.compress(req))
    assert res.succeeded()
    assert res.engine_id == "echo"
    assert res.engine_version == "0.1"


@pytest.mark.parametrize(
    "result, expected",
    [
        # success: content + ratio both set, no error
        (
            CompressionResult(
                engine_id="x", engine_version="1", original_tokens=10,
                compressed_content="hi", compression_ratio=0.5,
            ),
            True,
        ),
        # failure: error set
        (
            CompressionResult(
                engine_id="x", engine_version="1", original_tokens=10,
                error="gpu unreachable",
            ),
            False,
        ),
        # failure: no compressed_content
        (
            CompressionResult(
                engine_id="x", engine_version="1", original_tokens=10,
                compression_ratio=0.5,
            ),
            False,
        ),
        # failure: no ratio
        (
            CompressionResult(
                engine_id="x", engine_version="1", original_tokens=10,
                compressed_content="hi",
            ),
            False,
        ),
    ],
)
def test_result_succeeded(result, expected):
    assert result.succeeded() is expected


def test_supports_default_is_true():
    class Always(CompressionEngine):
        id = "always"
        label = "Always Eligible"

        async def compress(self, request):
            return CompressionResult(
                engine_id=self.id, engine_version=self.version, original_tokens=0
            )

    req = CompressionRequest(memory_id="m", content="")
    assert Always().supports(req) is True


def test_request_defaults():
    req = CompressionRequest(memory_id="m", content="x")
    assert req.owner_id == "default"
    assert req.target_ratio == BASE_CHUNK_RATIO
    assert req.identifier_policy is IdentifierPolicy.STRICT
    assert req.scoring_profile == "balanced"
    assert req.previous_summary is None
    assert req.metadata == {}
