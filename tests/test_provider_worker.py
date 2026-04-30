from __future__ import annotations

import inspect

import pytest

from mnemos.domain.graeae.provider_worker import (
    LocalProviderWorker,
    ProviderQueryRequest,
    ProviderQueryResponse,
    ProviderWorker,
)


class _Engine:
    def __init__(self):
        self.providers = {
            "openai": {
                "url": "https://example.invalid/v1/chat/completions",
                "model": "base-model",
                "weight": 0.8,
                "api": "openai",
                "key_name": "openai",
            }
        }
        self.calls = []

    async def _query_openai_compatible(
        self,
        provider,
        prompt,
        timeout,
        generation_params=None,
        request_params=None,
        messages=None,
    ):
        self.calls.append({
            "provider": provider,
            "prompt": prompt,
            "timeout": timeout,
            "generation_params": generation_params,
            "request_params": request_params,
            "messages": messages,
        })
        return {
            "status": "success",
            "response_text": "ok",
            "latency_ms": 0,
            "model_id": provider["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "cost": 0.012,
        }


def test_provider_query_request_constructor():
    request = ProviderQueryRequest(
        provider="openai",
        model="gpt-test",
        messages=[{"role": "user", "content": "hi"}],
        params={"prompt": "hi", "timeout": 30},
    )

    assert request.provider == "openai"
    assert request.model == "gpt-test"
    assert request.messages == [{"role": "user", "content": "hi"}]
    assert request.params["timeout"] == 30


def test_provider_query_response_constructor():
    payload = {"status": "success", "response_text": "ok", "model_id": "m"}
    response = ProviderQueryResponse(
        response_text="ok",
        latency_ms=12,
        status="success",
        cost=0.1,
        model_id_used="m",
        raw_provider_payload=payload,
    )

    assert response.response_text == "ok"
    assert response.latency_ms == 12
    assert response.status == "success"
    assert response.cost == 0.1
    assert response.model_id_used == "m"
    assert response.raw_provider_payload is payload


def test_provider_worker_protocol_shape():
    worker = LocalProviderWorker(_Engine())

    assert isinstance(worker, ProviderWorker)
    assert inspect.iscoroutinefunction(worker.__call__)


@pytest.mark.asyncio
async def test_local_provider_worker_happy_path_with_mocked_provider_call():
    engine = _Engine()
    worker = LocalProviderWorker(engine)
    messages = [{"role": "user", "content": "hello"}]
    request = ProviderQueryRequest(
        provider="openai",
        model="override-model",
        messages=messages,
        params={
            "prompt": "hello",
            "task_type": "reasoning",
            "timeout": 45,
            "generation_params": {"max_tokens": 7},
            "request_params": {"user": "u1"},
        },
    )

    response = await worker(request)

    assert response.status == "success"
    assert response.response_text == "ok"
    assert response.cost == 0.012
    assert response.model_id_used == "override-model"
    assert response.raw_provider_payload["latency_ms"] >= 0
    assert response.raw_provider_payload["final_score"] == 0.8
    assert response.raw_provider_payload["model_id"] == "override-model"
    assert engine.calls == [{
        "provider": {
            "url": "https://example.invalid/v1/chat/completions",
            "model": "override-model",
            "weight": 0.8,
            "api": "openai",
            "key_name": "openai",
        },
        "prompt": "hello",
        "timeout": 45,
        "generation_params": {"max_tokens": 7},
        "request_params": {"user": "u1"},
        "messages": messages,
    }]
