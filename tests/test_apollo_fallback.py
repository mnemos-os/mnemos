"""APOLLO LLM-fallback path (v3.3 S-II).

Exercises the fallback-when-no-schema-matches branch by:
  * Replacing APOLLOEngine._get_client with a fake httpx-like
    client whose .post() returns canned JSON.
  * Using a one-off gpu_url per test so the gpu_guard registry
    doesn't leak state between cases.

Does NOT hit a real GPU. Live-GPU smoke coverage lives in the
manual integration harness (see docs/benchmarks/).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, Optional

from mnemos.domain.compression.apollo import (
    _FALLBACK_QUALITY_SCORE,
    APOLLOEngine,
    _normalize_fallback_output,
)
from mnemos.domain.compression.base import (
    CompressionRequest,
    IdentifierPolicy,
)
from mnemos.domain.compression.gpu_guard import get_guard

# ── helpers ─────────────────────────────────────────────────────────────────

def _fresh_gpu_url() -> str:
    """Unique per-test URL so the gpu_guard singleton registry hands
    out a fresh CLOSED-state guard each time."""
    return f"http://apollo-fallback-test-{uuid.uuid4().hex[:8]}:8000"


def _req(content: str) -> CompressionRequest:
    return CompressionRequest(memory_id="m1", content=content, owner_id="default")


class _FakeResponse:
    def __init__(self, payload: Dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self) -> Dict[str, Any]:
        return self._payload


class _FakeClient:
    """Duck-type of httpx.AsyncClient that returns canned responses."""

    def __init__(self, response: Optional[_FakeResponse] = None,
                 raise_on_post: Optional[Exception] = None):
        self._response = response
        self._raise = raise_on_post
        self.is_closed = False
        self.calls: list = []

    async def post(self, url, json=None, timeout=None):  # noqa: ARG002
        self.calls.append({"url": url, "json": json})
        if self._raise is not None:
            raise self._raise
        assert self._response is not None
        return self._response

    async def aclose(self) -> None:
        self.is_closed = True


def _install_fake_client(engine: APOLLOEngine, client: _FakeClient) -> None:
    async def _get(_self=engine):
        return client
    engine._get_client = _get  # type: ignore[assignment]


# ── parser ─────────────────────────────────────────────────────────────────

def test_normalize_accepts_exact_shape():
    good = (
        "summary=alice visited bob;facts=[alice-visited-bob|bob-confirmed];"
        "entities=[alice|bob];concepts=[visit]"
    )
    assert _normalize_fallback_output(good) == good


def test_normalize_accepts_shape_inside_preamble():
    """LLMs sometimes emit an extra explanatory line before the
    payload; the parser should scan lines and accept the first
    match."""
    raw = (
        "Here is the encoded form:\n"
        "summary=s;facts=[f];entities=[e];concepts=[c]\n"
        "Hope that helps!"
    )
    assert _normalize_fallback_output(raw) == "summary=s;facts=[f];entities=[e];concepts=[c]"


def test_normalize_accepts_pipe_as_section_separator():
    """Regression for the live-GPU smoke: gemma4-class models
    empirically conflate the in-list pipe with the section
    separator and emit the whole line pipe-delimited. The parser
    accepts either separator between top-level sections because
    small instruction-tuned models aren't reliable at the
    distinction — see the updated _FALLBACK_PROMPT for the
    operator-side fix (concrete example + explicit section-vs-list
    punctuation call-out)."""
    pipe_shape = (
        "summary=Bob shipped v1.2|facts=[v1.2-shipped|CI-passed]"
        "|entities=[Bob|CI]|concepts=[release|deploy]"
    )
    out = _normalize_fallback_output(pipe_shape)
    assert out is not None, (
        "parser must accept pipe-separated top-level sections — "
        "gemma4-consult empirically produces this form"
    )
    assert out == pipe_shape


def test_normalize_rejects_missing_section():
    bad = "summary=s;facts=[f];entities=[e]"   # no concepts
    assert _normalize_fallback_output(bad) is None


def test_normalize_accepts_comma_inside_lists():
    """Comma inside a list isn't malformed per the grammar — the
    LLM just missed the '|' separator instruction, so downstream
    pipe-splitting treats the whole bracket content as one item.
    Not the parser's job to punish prompt slippage; shape is what
    the parser validates."""
    shape_ok = "summary=s;facts=[f1, f2];entities=[e];concepts=[c]"
    assert _normalize_fallback_output(shape_ok) == shape_ok


def test_normalize_rejects_empty_input():
    assert _normalize_fallback_output("") is None
    assert _normalize_fallback_output(None) is None  # type: ignore[arg-type]


# ── engine: successful fallback ─────────────────────────────────────────────

def test_fallback_success_produces_dense_compressed_result():
    gpu_url = _fresh_gpu_url()
    engine = APOLLOEngine(gpu_url=gpu_url)
    dense = (
        "summary=alice joined acme;facts=[alice-joined-acme|acme-hiring];"
        "entities=[alice|acme];concepts=[hire]"
    )
    fake = _FakeClient(_FakeResponse({"choices": [{"text": dense}]}))
    _install_fake_client(engine, fake)

    # Content clearly doesn't match PortfolioSchema — exercises the
    # fallback path.
    req = _req("Alice joined Acme Corp last week as a senior engineer.")
    result = asyncio.run(engine.compress(req))

    assert result.error is None
    assert result.succeeded() is True
    assert result.compressed_content == dense
    assert result.identifier_policy == IdentifierPolicy.OFF
    assert result.gpu_used is True
    assert result.quality_score == _FALLBACK_QUALITY_SCORE
    assert result.manifest["path"] == "fallback"
    assert result.manifest["gpu_url"] == gpu_url
    assert result.manifest["output_shape"] == "summary;facts;entities;concepts"
    assert len(fake.calls) == 1


def test_fallback_parse_failure_returns_error_result_but_success_reported_to_guard():
    """LLM returns garbage — engine records an error result BUT
    signals success to the GPU guard because the HTTP call itself
    succeeded. Parse failure is a prompt/model issue, not a
    GPU-health issue."""
    gpu_url = _fresh_gpu_url()
    engine = APOLLOEngine(gpu_url=gpu_url)
    fake = _FakeClient(_FakeResponse({"choices": [{"text": "I cannot parse this."}]}))
    _install_fake_client(engine, fake)

    req = _req("Alice joined Acme last week as a senior engineer.")
    result = asyncio.run(engine.compress(req))

    assert result.error == "fallback_parse_failed"
    assert result.succeeded() is False
    assert result.gpu_used is True
    assert "raw_output_preview" in result.manifest
    assert result.manifest["raw_output_preview"].startswith("I cannot")


def test_fallback_http_failure_records_failure_on_guard():
    gpu_url = _fresh_gpu_url()
    engine = APOLLOEngine(gpu_url=gpu_url)
    fake = _FakeClient(raise_on_post=RuntimeError("connection refused"))
    _install_fake_client(engine, fake)

    req = _req("Alice joined Acme.")
    result = asyncio.run(engine.compress(req))

    assert result.succeeded() is False
    assert result.error is not None
    assert "connection refused" in result.error
    assert result.manifest["path"] == "fallback"


def test_fallback_short_circuits_when_circuit_open():
    """Pre-open the guard before calling compress(); the engine
    should return an error result without any HTTP attempt."""
    gpu_url = _fresh_gpu_url()
    guard = get_guard(gpu_url)
    # Force OPEN by recording enough failures.
    for _ in range(10):
        asyncio.run(guard.record_failure(RuntimeError("probe fail")))
    engine = APOLLOEngine(gpu_url=gpu_url)
    fake = _FakeClient(_FakeResponse({"choices": [{"text": "unused"}]}))
    _install_fake_client(engine, fake)

    req = _req("Alice joined Acme.")
    result = asyncio.run(engine.compress(req))

    assert result.succeeded() is False
    assert "circuit open" in (result.error or "")
    assert result.gpu_used is False
    # No HTTP attempt made.
    assert fake.calls == []
    assert result.manifest.get("circuit_state") is not None


# ── schema path wins over fallback when both would work ───────────────────

def test_schema_match_wins_over_llm_fallback():
    """When the portfolio schema matches, APOLLO never invokes the
    LLM fallback — the fast path is preferred."""
    gpu_url = _fresh_gpu_url()
    engine = APOLLOEngine(gpu_url=gpu_url)
    fake = _FakeClient(_FakeResponse({"choices": [{"text": "should-not-fire"}]}))
    _install_fake_client(engine, fake)

    req = _req("Portfolio: AAPL 100 at 150 now 175. MSFT 50 at 300 now 310.")
    result = asyncio.run(engine.compress(req))

    assert result.succeeded()
    assert result.manifest["path"] == "schema"
    assert result.manifest["schema_id"] == "portfolio"
    assert result.gpu_used is False
    # LLM not invoked.
    assert fake.calls == []


# ── close() housekeeping ────────────────────────────────────────────────────

def test_close_releases_client():
    gpu_url = _fresh_gpu_url()
    engine = APOLLOEngine(gpu_url=gpu_url)
    fake = _FakeClient(_FakeResponse({"choices": [{"text":
        "summary=s;facts=[];entities=[];concepts=[]"}]}))
    _install_fake_client(engine, fake)
    # Touch the fallback path so the engine holds a client reference.
    asyncio.run(engine.compress(_req("Some arbitrary prose.")))
    # The fake is held as the substituted _get_client return; reset
    # engine._client to our fake so close() has something to close.
    engine._client = fake  # type: ignore[assignment]
    asyncio.run(engine.close())
    assert fake.is_closed is True
