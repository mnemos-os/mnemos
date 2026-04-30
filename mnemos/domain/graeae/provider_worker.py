from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass
class ProviderQueryRequest:
    provider: str
    model: Optional[str]
    messages: Optional[list[dict]]
    params: Dict[str, Any]


@dataclass
class ProviderQueryResponse:
    response_text: str
    latency_ms: int
    status: str
    cost: Optional[float]
    model_id_used: str
    raw_provider_payload: Dict[str, Any]


@runtime_checkable
class ProviderWorker(Protocol):
    async def __call__(self, request: ProviderQueryRequest) -> ProviderQueryResponse:
        ...


class LocalProviderWorker:
    def __init__(self, engine: Any):
        self._engine = engine

    async def __call__(self, request: ProviderQueryRequest) -> ProviderQueryResponse:
        provider_name = request.provider
        prompt = request.params["prompt"]
        timeout = request.params["timeout"]
        generation_params = request.params.get("generation_params")
        request_params = request.params.get("request_params")
        messages = request.messages
        model_override = request.model

        # Snapshot the provider config so a concurrent reload_from_registry
        # mutation can't tear the dict mid-dispatch. shallow copy is enough
        # because we only read scalar fields (model, url, weight, api,
        # key_name) and never mutate them here.
        provider = dict(self._engine.providers[provider_name])
        if model_override:
            provider["model"] = model_override
            # gemini's URL embeds the model name; rebuild for the override.
            if provider.get("api") == "gemini":
                provider["url"] = (
                    f"https://generativelanguage.googleapis.com/v1beta/"
                    f"models/{model_override}:generateContent"
                )
        start = datetime.now(timezone.utc)
        api = provider["api"]

        if api == "openai":
            response = await self._engine._query_openai_compatible(
                provider,
                prompt,
                timeout,
                generation_params=generation_params,
                request_params=request_params,
                messages=messages,
            )
        elif api == "anthropic":
            response = await self._engine._query_anthropic(
                provider,
                prompt,
                timeout,
                generation_params=generation_params,
                request_params=request_params,
                messages=messages,
            )
        elif api == "gemini":
            response = await self._engine._query_gemini(
                provider,
                prompt,
                timeout,
                generation_params=generation_params,
                request_params=request_params,
                messages=messages,
            )
        else:
            raise ValueError(f"Unknown api style '{api}' for provider '{provider_name}'")

        latency = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        response["latency_ms"] = latency
        response["final_score"] = provider["weight"]  # overridden by quality tracker in consult()
        return ProviderQueryResponse(
            response_text=response.get("response_text", ""),
            latency_ms=response["latency_ms"],
            status=response.get("status", ""),
            cost=response.get("cost"),
            model_id_used=response.get("model_id", provider["model"]),
            raw_provider_payload=response,
        )
