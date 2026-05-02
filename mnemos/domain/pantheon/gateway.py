"""Provider forwarding for PANTHEON v0.1."""

from __future__ import annotations

import json
import hashlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from mnemos.domain.graeae.api_keys import get_key
from mnemos.domain.graeae.engine import get_graeae_engine
from mnemos.domain.openai_compat.content import _content_text, _flatten_messages_for_prompt
from mnemos.domain.pantheon.router import RouteDecision

logger = logging.getLogger(__name__)
_IDENTITY_BODY_KEY = "_mnemos_upstream_identity"


class PantheonGatewayError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class UpstreamIdentity:
    user_id: str
    namespace: str
    session_id: str
    request_id: str

    @property
    def opaque_user(self) -> str:
        digest = hashlib.sha256(self.user_id.encode("utf-8")).hexdigest()[:16]
        return f"mnemos:{digest}"


def attach_upstream_identity(body: dict[str, Any], identity: UpstreamIdentity) -> dict[str, Any]:
    payload = dict(body)
    payload[_IDENTITY_BODY_KEY] = asdict(identity)
    return payload


def _pop_upstream_identity(payload: dict[str, Any]) -> UpstreamIdentity | None:
    raw = payload.pop(_IDENTITY_BODY_KEY, None)
    if not isinstance(raw, dict):
        return None
    try:
        return UpstreamIdentity(
            user_id=str(raw["user_id"]),
            namespace=str(raw["namespace"]),
            session_id=str(raw["session_id"]),
            request_id=str(raw["request_id"]),
        )
    except KeyError:
        return None


def _identity_headers(identity: UpstreamIdentity | None) -> dict[str, str]:
    if identity is None:
        return {}
    return {
        "X-MNEMOS-User-Id": identity.user_id,
        "X-MNEMOS-Namespace": identity.namespace,
        "X-MNEMOS-Session": identity.session_id,
        "X-MNEMOS-Request-Id": identity.request_id,
    }


def _provider_config(decision: RouteDecision) -> dict[str, Any]:
    engine = get_graeae_engine()
    cfg = dict(engine.providers.get(decision.provider, {}))
    if not cfg:
        raise PantheonGatewayError(503, f"provider {decision.provider!r} is not registered")
    if decision.model_id:
        cfg["model"] = decision.model_id
    return cfg


def _auth_headers(cfg: dict[str, Any], identity: UpstreamIdentity | None = None) -> dict[str, str]:
    key_name = cfg.get("key_name")
    api_key = get_key(str(key_name or ""))
    if not api_key:
        raise PantheonGatewayError(503, f"missing api_key for provider key_name={key_name!r}")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **_identity_headers(identity),
    }


def _chat_payload(decision: RouteDecision, body: dict[str, Any], *, stream: bool | None = None) -> dict[str, Any]:
    payload = dict(body)
    identity = _pop_upstream_identity(payload)
    if decision.model_id:
        payload["model"] = decision.model_id
    if stream is not None:
        payload["stream"] = stream
    if identity is not None:
        supplied_user = payload.get("user")
        if supplied_user is not None and supplied_user != identity.opaque_user:
            logger.warning(
                "[PANTHEON] client-supplied OpenAI user field overridden "
                "for request_id=%s",
                identity.request_id,
            )
        payload["user"] = identity.opaque_user
    return payload


def _embeddings_url(cfg: dict[str, Any]) -> str:
    if cfg.get("embeddings_url"):
        return str(cfg["embeddings_url"])
    url = str(cfg.get("url") or "")
    if "/chat/completions" in url:
        return url.replace("/chat/completions", "/embeddings")
    return url.rstrip("/") + "/embeddings"


async def forward_chat_completion(decision: RouteDecision, body: dict[str, Any]) -> dict[str, Any]:
    if decision.route_type == "consensus":
        return await consensus_chat_completion(decision, body)

    cfg = _provider_config(decision)
    if cfg.get("api", "openai") != "openai":
        return await _graeae_chat_completion(decision, body)

    async with httpx.AsyncClient(timeout=cfg.get("timeout", 200)) as client:
        payload = _chat_payload(decision, body, stream=False)
        response = await client.post(
            str(cfg["url"]),
            json=payload,
            headers=_auth_headers(cfg, _pop_upstream_identity(dict(body))),
        )
    if response.status_code >= 400:
        raise PantheonGatewayError(response.status_code, response.text[:500])
    data = response.json()
    data.setdefault("model", decision.model_id)
    return data


async def stream_chat_completion(decision: RouteDecision, body: dict[str, Any]) -> AsyncIterator[bytes]:
    if decision.route_type == "consensus":
        async for event in consensus_chat_completion_stream(decision, body):
            yield event
        return

    cfg = _provider_config(decision)
    if cfg.get("api", "openai") != "openai":
        async for event in _graeae_chat_completion_stream(decision, body):
            yield event
        return

    client = httpx.AsyncClient(timeout=None)
    try:
        payload = _chat_payload(decision, body, stream=True)
        async with client.stream(
            "POST",
            str(cfg["url"]),
            json=payload,
            headers=_auth_headers(cfg, _pop_upstream_identity(dict(body))),
        ) as response:
            if response.status_code >= 400:
                body_bytes = await response.aread()
                raise PantheonGatewayError(response.status_code, body_bytes[:500].decode("utf-8", "replace"))
            async for chunk in response.aiter_bytes():
                yield chunk
    finally:
        await client.aclose()


async def forward_embeddings(decision: RouteDecision, body: dict[str, Any]) -> dict[str, Any]:
    if decision.route_type == "consensus":
        raise PantheonGatewayError(400, "consensus aliases are not valid for embeddings")
    cfg = _provider_config(decision)
    async with httpx.AsyncClient(timeout=cfg.get("timeout", 200)) as client:
        payload = _chat_payload(decision, body, stream=None)
        response = await client.post(
            _embeddings_url(cfg),
            json=payload,
            headers=_auth_headers(cfg, _pop_upstream_identity(dict(body))),
        )
    if response.status_code >= 400:
        raise PantheonGatewayError(response.status_code, response.text[:500])
    data = response.json()
    data.setdefault("model", decision.model_id)
    return data


async def _graeae_chat_completion(decision: RouteDecision, body: dict[str, Any]) -> dict[str, Any]:
    payload = _chat_payload(decision, body, stream=None)
    messages = payload.get("messages") or []
    prompt = _flatten_messages_for_prompt(messages)
    engine = get_graeae_engine()
    result = await engine.route(
        decision.provider,
        decision.model_id or "",
        prompt,
        task_type="reasoning",
        timeout=30,
        generation_params=_generation_params(payload),
        request_params=_request_params(payload),
        messages=messages,
    )
    if result.get("status") != "success":
        raise PantheonGatewayError(503, result.get("error") or "provider unavailable")
    return _openai_chat_response(decision.model_id or decision.alias, result.get("choices"), result.get("response_text", ""), messages)


async def _graeae_chat_completion_stream(decision: RouteDecision, body: dict[str, Any]) -> AsyncIterator[bytes]:
    response = await _graeae_chat_completion(decision, body)
    yield _stream_event({
        "id": response["id"],
        "object": "chat.completion.chunk",
        "created": response["created"],
        "model": response["model"],
        "choices": [{"index": 0, "delta": {"role": "assistant"}}],
    })
    content = response["choices"][0]["message"].get("content") or ""
    if content:
        yield _stream_event({
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [{"index": 0, "delta": {"content": content}}],
        })
    yield _stream_event({
        "id": response["id"],
        "object": "chat.completion.chunk",
        "created": response["created"],
        "model": response["model"],
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    yield b"data: [DONE]\n\n"


async def consensus_chat_completion(decision: RouteDecision, body: dict[str, Any]) -> dict[str, Any]:
    messages = body.get("messages") or []
    prompt = _flatten_messages_for_prompt(messages)
    engine = get_graeae_engine()
    result = await engine.consult(
        prompt,
        task_type=decision.task_type or "reasoning",
        timeout=body.get("timeout", 180),
        mode="auto",
    )
    content = result.get("consensus_response") or ""
    return _openai_chat_response(decision.alias, None, content, messages)


async def consensus_chat_completion_stream(decision: RouteDecision, body: dict[str, Any]) -> AsyncIterator[bytes]:
    response = await consensus_chat_completion(decision, body)
    yield _stream_event({
        "id": response["id"],
        "object": "chat.completion.chunk",
        "created": response["created"],
        "model": response["model"],
        "choices": [{"index": 0, "delta": {"role": "assistant"}}],
    })
    content = response["choices"][0]["message"].get("content") or ""
    if content:
        yield _stream_event({
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [{"index": 0, "delta": {"content": content}}],
        })
    yield _stream_event({
        "id": response["id"],
        "object": "chat.completion.chunk",
        "created": response["created"],
        "model": response["model"],
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })
    yield b"data: [DONE]\n\n"


def _generation_params(body: dict[str, Any]) -> dict[str, Any]:
    return {key: body[key] for key in ("temperature", "max_tokens", "top_p") if body.get(key) is not None}


def _request_params(body: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "tools", "tool_choice", "response_format", "stop", "n",
        "presence_penalty", "frequency_penalty", "user",
    )
    return {key: body[key] for key in fields if body.get(key) is not None}


def _openai_chat_response(
    model: str,
    choices: list[dict[str, Any]] | None,
    content: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    created = int(time.time())
    normalized_choices = choices or [
        {
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }
    ]
    prompt_tokens = sum(len(_content_text(message.get("content")).split()) for message in messages)
    completion_tokens = len(content.split())
    return {
        "id": f"chatcmpl-pantheon-{created}",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": normalized_choices,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _stream_event(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")
