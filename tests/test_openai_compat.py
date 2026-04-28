from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from api.auth import UserContext
from api.handlers import openai_compat


def _user() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="default", authenticated=True,
    )


class _Conn:
    def __init__(self, *, row=None):
        self._row = row

    async def fetchrow(self, sql: str, *args):
        return self._row


class _PoolCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


def _install_pool(monkeypatch, conn):
    import api.lifecycle as lc

    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


class _FakeGraeae:
    def __init__(self, providers=None):
        self.providers = providers or {
            "openai": {
                "api": "openai",
                "model": "gpt-5.4",
                "url": "https://api.openai.com/v1/chat/completions",
                "key_name": "openai",
            }
        }
        self.route_calls = []
        self.stream_calls = []

    async def route(self, *args, **kwargs):
        self.route_calls.append((args, kwargs))
        return {
            "status": "success",
            "response_text": "ok",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
        }

    async def route_stream(self, *args, **kwargs):
        self.stream_calls.append((args, kwargs))
        yield {"index": 0, "content": "hel"}
        yield {"index": 0, "content": "lo"}


async def _no_context(*args, **kwargs):
    return []


def _install_gateway(monkeypatch, fake: _FakeGraeae, provider: str = "openai"):
    async def _resolver(model: str):
        return provider

    monkeypatch.setattr(openai_compat, "_search_mnemos_context", _no_context)
    monkeypatch.setattr(openai_compat, "_resolve_provider_for_model", _resolver)
    monkeypatch.setattr(openai_compat, "get_graeae_engine", lambda: fake)


def test_temperature_max_tokens_top_p_propagate(monkeypatch):
    fake = _FakeGraeae()
    _install_gateway(monkeypatch, fake)

    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
        temperature=0.2,
        max_tokens=100,
        top_p=0.9,
    )

    asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))

    assert fake.route_calls
    kwargs = fake.route_calls[0][1]
    assert kwargs["generation_params"] == {
        "temperature": 0.2,
        "max_tokens": 100,
        "top_p": 0.9,
    }


def test_stream_returns_sse(monkeypatch):
    fake = _FakeGraeae()
    _install_gateway(monkeypatch, fake)

    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
        stream=True,
    )

    response = asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))
    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"

    async def _collect():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    body = asyncio.run(_collect())
    events = [line.removeprefix("data: ") for line in body.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    decoded = [json.loads(event) for event in events[:-1]]
    assert decoded[0]["choices"][0]["delta"]["role"] == "assistant"
    assert decoded[1]["choices"][0]["delta"]["content"] == "hel"
    assert decoded[2]["choices"][0]["delta"]["content"] == "lo"
    assert decoded[-1]["choices"][0]["finish_reason"] == "stop"


class _Resp:
    status_code = 200
    text = "{}"

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Client:
    def __init__(self, data):
        self.data = data
        self.payloads = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.payloads.append(json)
        return _Resp(self.data)


def _engine_with_client(monkeypatch, data):
    from graeae import engine as engine_module

    engine = engine_module.GraeaeEngine()
    client = _Client(data)

    async def _client():
        return client

    monkeypatch.setattr(engine_module, "get_key", lambda key_name: "sk-test")
    monkeypatch.setattr(engine, "_get_client", _client)
    return engine, client


def test_tools_passthrough_supported_provider(monkeypatch):
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
    ]
    engine, client = _engine_with_client(
        monkeypatch,
        {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
                    "finish_reason": "tool_calls",
                }
            ]
        },
    )
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]

    result = asyncio.run(engine._query_openai_compatible(
        {
            "url": "https://api.openai.com/v1/chat/completions",
            "model": "gpt-5.4",
            "key_name": "openai",
        },
        "hello",
        30,
        request_params={"tools": tools, "tool_choice": "auto"},
    ))

    assert client.payloads[0]["tools"] == tools
    assert client.payloads[0]["tool_choice"] == "auto"
    assert result["choices"][0]["message"]["tool_calls"] == tool_calls


def test_tools_rejected_unsupported_provider(monkeypatch):
    fake = _FakeGraeae(
        providers={
            "groq": {
                "api": "openai",
                "model": "llama-3.3-70b-versatile",
                "url": "https://api.groq.com/openai/v1/chat/completions",
                "key_name": "groq",
            }
        }
    )
    _install_gateway(monkeypatch, fake, provider="groq")
    req = openai_compat.ChatCompletionRequest(
        model="llama-3.3-70b-versatile",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))
    assert exc.value.status_code == 400
    assert "does not support tool_calls" in exc.value.detail


def test_response_format_passthrough(monkeypatch):
    engine, client = _engine_with_client(
        monkeypatch,
        {"choices": [{"message": {"role": "assistant", "content": "{}"}, "finish_reason": "stop"}]},
    )

    asyncio.run(engine._query_openai_compatible(
        {
            "url": "https://api.openai.com/v1/chat/completions",
            "model": "gpt-5.4",
            "key_name": "openai",
        },
        "json please",
        30,
        request_params={"response_format": {"type": "json_object"}},
    ))

    assert client.payloads[0]["response_format"] == {"type": "json_object"}


def test_unknown_field_handling(monkeypatch):
    engine, client = _engine_with_client(
        monkeypatch,
        {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]},
    )

    asyncio.run(engine._query_openai_compatible(
        {
            "url": "https://api.openai.com/v1/chat/completions",
            "model": "gpt-5.4",
            "key_name": "openai",
        },
        "hello",
        30,
        request_params={"presence_penalty": 1.0},
    ))
    assert client.payloads[0]["presence_penalty"] == 1.0

    fake = _FakeGraeae(
        providers={
            "claude": {
                "api": "anthropic",
                "model": "claude-opus-4-6",
                "url": "https://api.anthropic.com/v1/messages",
                "key_name": "claude",
            }
        }
    )
    _install_gateway(monkeypatch, fake, provider="claude")
    req = openai_compat.ChatCompletionRequest(
        model="claude-opus-4-6",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
        presence_penalty=1.0,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))
    assert exc.value.status_code == 400
    assert "does not support penalties" in exc.value.detail


def test_multimodal_content_blocks(monkeypatch):
    content = [
        openai_compat.ContentBlock(type="text", text="hi"),
        openai_compat.ContentBlock(type="image_url", image_url={"url": "https://example.test/image.png"}),
    ]
    fake = _FakeGraeae()
    _install_gateway(monkeypatch, fake, provider="openai")
    req = openai_compat.ChatCompletionRequest(
        model="gpt-5-vision",
        messages=[openai_compat.ChatMessage(role="user", content=content)],
    )

    asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))
    assert isinstance(fake.route_calls[0][1]["messages"][0]["content"], list)

    text_only = _FakeGraeae(
        providers={
            "groq": {
                "api": "openai",
                "model": "llama-3.3-70b-versatile",
                "url": "https://api.groq.com/openai/v1/chat/completions",
                "key_name": "groq",
            }
        }
    )
    _install_gateway(monkeypatch, text_only, provider="groq")
    bad_req = openai_compat.ChatCompletionRequest(
        model="llama-3.3-70b-versatile",
        messages=[openai_compat.ChatMessage(role="user", content=content)],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.chat_completions(bad_req, authorization=None, user=_user()))
    assert exc.value.status_code == 400
    assert "does not support multimodal content blocks" in exc.value.detail


def test_unknown_model_returns_404(monkeypatch):
    _install_pool(monkeypatch, _Conn(row=None))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.get_model("nonexistent-model", authorization=None, user=_user()))

    assert exc.value.status_code == 404
    assert exc.value.detail == "model not found"


def test_registered_model_lookup_works(monkeypatch):
    _install_pool(monkeypatch, _Conn(row={"provider": "openai"}))

    result = asyncio.run(openai_compat.get_model("gpt-5.4", authorization=None, user=_user()))

    assert result.id == "gpt-5.4"
    assert result.owned_by == "OpenAI"
