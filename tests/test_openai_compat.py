from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.auth import UserContext, get_current_user
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
        yield {"index": 0, "finish_reason": "stop"}


async def _no_context(*args, **kwargs):
    return []


def _install_gateway(monkeypatch, fake: _FakeGraeae, provider: str = "openai"):
    async def _resolver(model: str):
        return provider

    monkeypatch.setattr(openai_compat, "_search_mnemos_context", _no_context)
    monkeypatch.setattr(openai_compat, "_resolve_provider_for_model", _resolver)
    monkeypatch.setattr(openai_compat, "get_graeae_engine", lambda: fake)


def _sse_events(body: str) -> list[str]:
    return [line.removeprefix("data: ") for line in body.splitlines() if line.startswith("data: ")]


async def _collect_stream_body(response: StreamingResponse) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


async def _chat_stream_response_and_body(
    request: openai_compat.ChatCompletionRequest,
) -> tuple[StreamingResponse, str]:
    response = await openai_compat.chat_completions(request, authorization=None, user=_user())
    assert isinstance(response, StreamingResponse)
    body = await _collect_stream_body(response)
    return response, body


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("logit_bias", {"50256": -100}),
        ("seed", 1234),
        ("parallel_tool_calls", False),
        ("stream_options", {"include_usage": True}),
        ("logprobs", True),
    ],
)
def test_chat_request_rejects_unsupported_openai_fields(field, value):
    payload = {
        "model": "gpt-5.4",
        "messages": [{"role": "user", "content": "hello"}],
        field: value,
    }

    with pytest.raises(ValidationError) as exc:
        openai_compat.ChatCompletionRequest.model_validate_json(json.dumps(payload))

    assert field in str(exc.value)


def test_chat_message_rejects_unknown_nested_fields():
    payload = {
        "model": "gpt-5.4",
        "messages": [{"role": "user", "content": "hello", "legacy": True}],
    }

    with pytest.raises(ValidationError) as exc:
        openai_compat.ChatCompletionRequest.model_validate_json(json.dumps(payload))

    assert "legacy" in str(exc.value)


def test_content_block_and_tool_reject_unknown_nested_fields():
    bad_content = {
        "model": "gpt-5.4",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hello", "legacy": True}],
            }
        ],
    }
    with pytest.raises(ValidationError) as content_exc:
        openai_compat.ChatCompletionRequest.model_validate_json(json.dumps(bad_content))
    assert "legacy" in str(content_exc.value)

    bad_tool = {
        "model": "gpt-5.4",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "lookup", "parameters": {"type": "object"}, "legacy": True},
            }
        ],
    }
    with pytest.raises(ValidationError) as tool_exc:
        openai_compat.ChatCompletionRequest.model_validate_json(json.dumps(bad_tool))
    assert "legacy" in str(tool_exc.value)


def test_chat_message_name_propagates_to_openai_provider(monkeypatch):
    fake = _FakeGraeae()
    _install_gateway(monkeypatch, fake)
    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[openai_compat.ChatMessage(role="user", name="alice", content="hello")],
    )

    asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))

    assert fake.route_calls[0][1]["messages"][0]["name"] == "alice"


def test_chat_message_function_call_rejected_with_migration_hint(monkeypatch):
    fake = _FakeGraeae()
    _install_gateway(monkeypatch, fake)
    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[
            openai_compat.ChatMessage(
                role="assistant",
                content=None,
                function_call={"name": "lookup", "arguments": "{}"},
            ),
            openai_compat.ChatMessage(role="user", content="hello"),
        ],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))

    assert exc.value.status_code == 400
    assert "tool_calls" in exc.value.detail


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

    response, body = asyncio.run(_chat_stream_response_and_body(req))
    assert response.media_type == "text/event-stream"

    events = _sse_events(body)
    assert events[-1] == "[DONE]"
    decoded = [json.loads(event) for event in events[:-1]]
    assert decoded[0]["choices"][0]["delta"]["role"] == "assistant"
    assert decoded[1]["choices"][0]["delta"]["content"] == "hel"
    assert decoded[2]["choices"][0]["delta"]["content"] == "lo"
    assert decoded[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_preflight_failure_returns_error_before_sse(monkeypatch):
    class _FailingGraeae(_FakeGraeae):
        async def route_stream(self, *args, **kwargs):
            self.stream_calls.append((args, kwargs))
            if False:
                yield {}
            raise RuntimeError("missing api_key for provider 'openai'")

    fake = _FailingGraeae()
    _install_gateway(monkeypatch, fake)
    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
        stream=True,
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))

    assert exc.value.status_code == 503
    assert "missing api_key" in exc.value.detail


def test_stream_midstream_failure_emits_error_and_done(monkeypatch):
    class _MidstreamFailGraeae(_FakeGraeae):
        async def route_stream(self, *args, **kwargs):
            self.stream_calls.append((args, kwargs))
            yield {"index": 0, "content": "hel"}
            raise RuntimeError("upstream closed early")

    fake = _MidstreamFailGraeae()
    _install_gateway(monkeypatch, fake)
    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
        stream=True,
    )

    _response, body = asyncio.run(_chat_stream_response_and_body(req))
    events = _sse_events(body)

    assert events[-1] == "[DONE]"
    error_event = json.loads(events[-2])
    assert error_event["error"]["type"] == "provider_stream_error"
    assert "upstream closed early" in error_event["error"]["message"]


def test_provider_sse_error_first_frame_returns_http_error_and_records_failure(monkeypatch):
    _engine, _client, breaker, quality, concurrency = _stream_engine_gateway(
        monkeypatch,
        ['data: {"error":{"message":"foo","type":"invalid_request_error"}}'],
    )
    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
        stream=True,
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))

    assert exc.value.status_code == 400
    assert "foo" in exc.value.detail
    assert breaker.failures == ["openai"]
    assert breaker.successes == []
    assert quality.failures == ["openai"]
    assert quality.successes == []
    assert concurrency.released == ["openai"]


def test_provider_sse_error_midstream_emits_error_event_done_and_records_failure(monkeypatch):
    _engine, _client, breaker, quality, concurrency = _stream_engine_gateway(
        monkeypatch,
        [
            'data: {"choices":[{"index":0,"delta":{"content":"hel"}}]}',
            'data: {"error":{"message":"foo","type":"invalid_request_error"}}',
        ],
    )
    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
        stream=True,
    )

    _response, body = asyncio.run(_chat_stream_response_and_body(req))
    events = _sse_events(body)

    assert events[-1] == "[DONE]"
    error_event = json.loads(events[-2])
    assert error_event["error"] == {"message": "foo", "type": "invalid_request_error"}
    assert breaker.failures == ["openai"]
    assert breaker.successes == []
    assert quality.failures == ["openai"]
    assert quality.successes == []
    assert concurrency.released == ["openai"]


def test_stream_finish_reason_tool_calls_propagates(monkeypatch):
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
    ]

    class _ToolStreamGraeae(_FakeGraeae):
        async def route_stream(self, *args, **kwargs):
            self.stream_calls.append((args, kwargs))
            yield {"index": 0, "tool_calls": tool_calls}
            yield {"index": 0, "finish_reason": "tool_calls"}

    fake = _ToolStreamGraeae()
    _install_gateway(monkeypatch, fake)
    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
        stream=True,
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
    )

    _response, body = asyncio.run(_chat_stream_response_and_body(req))
    events = _sse_events(body)
    decoded = [json.loads(event) for event in events[:-1]]

    assert decoded[-1]["choices"][0]["finish_reason"] == "tool_calls"


def test_stream_n_two_gets_per_choice_role_delta_and_terminal(monkeypatch):
    class _TwoChoiceStreamGraeae(_FakeGraeae):
        async def route_stream(self, *args, **kwargs):
            self.stream_calls.append((args, kwargs))
            yield {"index": 0, "content": "alpha"}
            yield {"index": 1, "content": "beta"}
            yield {"index": 0, "finish_reason": "length"}
            yield {"index": 1, "finish_reason": "stop"}

    fake = _TwoChoiceStreamGraeae()
    _install_gateway(monkeypatch, fake)
    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
        stream=True,
        n=2,
    )

    _response, body = asyncio.run(_chat_stream_response_and_body(req))
    events = _sse_events(body)
    decoded = [json.loads(event) for event in events[:-1]]
    roles = {
        item["choices"][0]["index"]
        for item in decoded
        if item["choices"][0]["delta"].get("role") == "assistant"
    }
    content = {
        item["choices"][0]["index"]: item["choices"][0]["delta"]["content"]
        for item in decoded
        if "content" in item["choices"][0]["delta"]
    }
    finishes = {
        item["choices"][0]["index"]: item["choices"][0]["finish_reason"]
        for item in decoded
        if item["choices"][0].get("finish_reason")
    }

    assert roles == {0, 1}
    assert content == {0: "alpha", 1: "beta"}
    assert finishes == {0: "length", 1: "stop"}


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


class _StreamResp:
    status_code = 200

    def __init__(self, lines):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class _StreamClient:
    def __init__(self, lines):
        self.lines = lines
        self.payloads = []

    def stream(self, method, url, json=None, headers=None, timeout=None):
        self.payloads.append(json)
        return _StreamResp(self.lines)


class _RecorderBreaker:
    def __init__(self):
        self.successes = []
        self.failures = []

    def is_allowed(self, name):
        return True

    def record_success(self, name):
        self.successes.append(name)

    def record_failure(self, name):
        self.failures.append(name)


class _RecorderRateLimiter:
    def is_allowed(self, name):
        return True


class _RecorderQuality:
    def __init__(self):
        self.successes = []
        self.failures = []

    def record_success(self, name, latency):
        self.successes.append((name, latency))

    def record_failure(self, name):
        self.failures.append(name)


class _RecorderConcurrency:
    def __init__(self):
        self.released = []

    async def acquire(self, name):
        return True

    def release(self, name):
        self.released.append(name)


def _engine_with_client(monkeypatch, data):
    from graeae import engine as engine_module

    engine = engine_module.GraeaeEngine()
    client = _Client(data)

    async def _client():
        return client

    monkeypatch.setattr(engine_module, "get_key", lambda key_name: "sk-test")
    monkeypatch.setattr(engine, "_get_client", _client)
    return engine, client


def _stream_engine_gateway(monkeypatch, lines):
    from graeae import engine as engine_module

    engine = engine_module.GraeaeEngine()
    engine.providers = {
        "openai": {
            "api": "openai",
            "url": "https://api.openai.com/v1/chat/completions",
            "model": "gpt-5.4",
            "key_name": "openai",
            "weight": 0.9,
        }
    }
    client = _StreamClient(lines)
    breaker = _RecorderBreaker()
    quality = _RecorderQuality()
    concurrency = _RecorderConcurrency()
    engine._circuit_breakers = breaker
    engine._rate_limiters = _RecorderRateLimiter()
    engine._quality = quality
    engine._concurrency = concurrency

    async def _client():
        return client

    monkeypatch.setattr(engine_module, "get_key", lambda key_name: "sk-test")
    monkeypatch.setattr(engine, "_get_client", _client)
    _install_gateway(monkeypatch, engine)
    return engine, client, breaker, quality, concurrency


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


def test_anthropic_multiturn_tool_history_preserves_tool_identity(monkeypatch):
    engine, client = _engine_with_client(
        monkeypatch,
        {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"},
    )
    messages = [
        {"role": "user", "content": "what is the weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_weather",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{\"city\":\"SF\"}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_weather", "content": "sunny"},
    ]

    asyncio.run(engine._query_anthropic(
        {
            "api": "anthropic",
            "url": "https://api.anthropic.com/v1/messages",
            "model": "claude-opus-4-6",
            "key_name": "claude",
        },
        "ignored when messages are present",
        30,
        messages=messages,
    ))

    anthropic_messages = client.payloads[0]["messages"]
    assert anthropic_messages[1] == {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "call_weather",
                "name": "get_weather",
                "input": {"city": "SF"},
            }
        ],
    }
    assert anthropic_messages[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_weather",
                "content": "sunny",
            }
        ],
    }


def test_anthropic_tool_choice_required_and_function_selector(monkeypatch):
    engine, client = _engine_with_client(
        monkeypatch,
        {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn"},
    )
    provider = {
        "api": "anthropic",
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-opus-4-6",
        "key_name": "claude",
    }
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]

    asyncio.run(engine._query_anthropic(
        provider,
        "hello",
        30,
        request_params={"tools": tools, "tool_choice": "required"},
    ))
    asyncio.run(engine._query_anthropic(
        provider,
        "hello",
        30,
        request_params={"tools": tools, "tool_choice": {"type": "function", "function": {"name": "lookup"}}},
    ))

    assert client.payloads[0]["tool_choice"] == {"type": "any"}
    assert client.payloads[1]["tool_choice"] == {"type": "tool", "name": "lookup"}


def test_anthropic_tool_choice_unsupported_string_rejected_before_dispatch(monkeypatch):
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
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        tool_choice="garbage_unsupported",
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))

    assert exc.value.status_code == 400
    assert "garbage_unsupported" in exc.value.detail
    assert fake.route_calls == []


@pytest.mark.parametrize("role", ["tool", "developer", "random_typo"])
def test_gemini_unsupported_roles_rejected_before_dispatch(monkeypatch, role):
    fake = _FakeGraeae(
        providers={
            "gemini": {
                "api": "gemini",
                "model": "gemini-test",
                "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent",
                "key_name": "gemini",
            }
        }
    )
    _install_gateway(monkeypatch, fake, provider="gemini")
    req = openai_compat.ChatCompletionRequest(
        model="gemini-test",
        messages=[
            openai_compat.ChatMessage(role="user", content="hello"),
            openai_compat.ChatMessage(role=role, content="should not be reclassified"),
        ],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))

    assert exc.value.status_code == 400
    assert exc.value.detail == (
        f"provider gemini does not support role={role}; supported: system, user, assistant"
    )
    assert fake.route_calls == []


def test_anthropic_developer_role_rejected_before_dispatch(monkeypatch):
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
        messages=[
            openai_compat.ChatMessage(role="user", content="hello"),
            openai_compat.ChatMessage(role="developer", content="not supported by anthropic"),
        ],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))

    assert exc.value.status_code == 400
    assert exc.value.detail == (
        "provider claude does not support role=developer; supported: system, user, assistant, tool"
    )
    assert fake.route_calls == []


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


@pytest.mark.parametrize(
    ("gemini_reason", "openai_reason"),
    [
        ("MAX_TOKENS", "length"),
        ("SAFETY", "content_filter"),
        ("STOP", "stop"),
        ("unexpected_new_reason", "stop"),
    ],
)
def test_gemini_finish_reason_normalized_non_streaming(monkeypatch, gemini_reason, openai_reason):
    engine, _client = _engine_with_client(
        monkeypatch,
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": "done"}]},
                    "finishReason": gemini_reason,
                }
            ]
        },
    )

    result = asyncio.run(engine._query_gemini(
        {
            "api": "gemini",
            "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent",
            "model": "gemini-test",
            "key_name": "gemini",
        },
        "hello",
        30,
    ))

    assert result["choices"][0]["finish_reason"] == openai_reason


def test_gemini_finish_reason_normalized_streaming_fallback(monkeypatch):
    engine, _client = _engine_with_client(
        monkeypatch,
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": "done"}]},
                    "finishReason": "MAX_TOKENS",
                }
            ]
        },
    )

    async def _collect():
        return [chunk async for chunk in engine.route_stream("gemini", "gemini-test", "hello")]

    chunks = asyncio.run(_collect())

    assert chunks[-1]["finish_reason"] == "length"


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


def test_provider_refusal_response_field_is_preserved(monkeypatch):
    class _RefusalGraeae(_FakeGraeae):
        async def route(self, *args, **kwargs):
            self.route_calls.append((args, kwargs))
            return {
                "status": "success",
                "response_text": "",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "refusal": "I cannot comply with that request.",
                        },
                        "finish_reason": "stop",
                    }
                ],
            }

    fake = _RefusalGraeae()
    _install_gateway(monkeypatch, fake)
    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
    )

    result = asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))

    assert result.choices[0].message.refusal == "I cannot comply with that request."


def test_provider_unknown_response_message_field_fails_closed(monkeypatch):
    class _UnknownFieldGraeae(_FakeGraeae):
        async def route(self, *args, **kwargs):
            self.route_calls.append((args, kwargs))
            return {
                "status": "success",
                "response_text": "ok",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                            "new_provider_field": {"opaque": True},
                        },
                        "finish_reason": "stop",
                    }
                ],
            }

    fake = _UnknownFieldGraeae()
    _install_gateway(monkeypatch, fake)
    req = openai_compat.ChatCompletionRequest(
        model="gpt-5.4",
        messages=[openai_compat.ChatMessage(role="user", content="hello")],
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(openai_compat.chat_completions(req, authorization=None, user=_user()))

    assert exc.value.status_code == 502
    assert "unsupported response field new_provider_field" in exc.value.detail


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


def test_chat_completion_unknown_model_returns_model_not_found_on_wire(monkeypatch):
    async def _unknown_model(model: str):
        return None

    monkeypatch.setattr(openai_compat, "_search_mnemos_context", _no_context)
    monkeypatch.setattr(openai_compat, "_resolve_provider_for_model", _unknown_model)

    from api_server import app

    async def _override_user():
        return _user()

    app.dependency_overrides[get_current_user] = _override_user
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "nonexistent-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        client.close()
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "message": "The model `nonexistent-model` does not exist or you do not have access to it.",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }


def test_registered_model_lookup_works(monkeypatch):
    _install_pool(monkeypatch, _Conn(row={"provider": "openai"}))

    result = asyncio.run(openai_compat.get_model("gpt-5.4", authorization=None, user=_user()))

    assert result.id == "gpt-5.4"
    assert result.owned_by == "OpenAI"
