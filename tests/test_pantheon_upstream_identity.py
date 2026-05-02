from __future__ import annotations

from mnemos.domain.pantheon.router import RouteDecision


def _decision() -> RouteDecision:
    return RouteDecision(
        alias="cheap-chat",
        provider="cheap",
        model_id="cheap-chat",
        route_type="direct",
        reason="test",
        model={"usage_tier": "agentic_ok"},
    )


def test_openai_payload_overrides_client_supplied_user(caplog):
    from mnemos.domain.pantheon import gateway

    identity = gateway.UpstreamIdentity(
        user_id="alice",
        namespace="default",
        session_id="session-1",
        request_id="request-1",
    )

    payload = gateway._chat_payload(
        _decision(),
        gateway.attach_upstream_identity(
            {
                "model": "cheap-chat",
                "messages": [{"role": "user", "content": "hi"}],
                "user": "spoofed-user",
            },
            identity,
        ),
        stream=False,
    )

    assert payload["user"] == identity.opaque_user
    assert payload["stream"] is False
    assert "_mnemos_upstream_identity" not in payload
    assert "overridden" in caplog.text


def test_upstream_forward_attaches_identity_headers(monkeypatch):
    import asyncio

    from mnemos.domain.pantheon import gateway

    recorded: dict = {}

    class _Response:
        status_code = 200

        def json(self):
            return {"id": "chatcmpl-test", "choices": [], "usage": {}}

    class _Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def post(self, url, *, json, headers):
            recorded["url"] = url
            recorded["json"] = json
            recorded["headers"] = headers
            return _Response()

    class _Engine:
        providers = {
            "cheap": {
                "url": "https://provider.example/v1/chat/completions",
                "model": "cheap-chat",
                "api": "openai",
                "key_name": "openai",
            }
        }

    identity = gateway.UpstreamIdentity(
        user_id="alice",
        namespace="default",
        session_id="session-1",
        request_id="request-1",
    )
    monkeypatch.setattr(gateway.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(gateway, "get_graeae_engine", lambda: _Engine())
    monkeypatch.setattr(gateway, "get_key", lambda _key_name: "provider-key")

    asyncio.run(gateway.forward_chat_completion(
        _decision(),
        gateway.attach_upstream_identity(
            {"model": "cheap-chat", "messages": [{"role": "user", "content": "hi"}]},
            identity,
        ),
    ))

    assert recorded["headers"]["Authorization"] == "Bearer provider-key"
    assert recorded["headers"]["X-MNEMOS-User-Id"] == "alice"
    assert recorded["headers"]["X-MNEMOS-Namespace"] == "default"
    assert recorded["headers"]["X-MNEMOS-Session"] == "session-1"
    assert recorded["headers"]["X-MNEMOS-Request-Id"] == "request-1"
    assert recorded["json"]["user"] == identity.opaque_user


def test_routing_audit_metadata_includes_forwarded_identity():
    from mnemos.domain.pantheon.routing_log import routing_payload

    payload, metadata = routing_payload(
        request_id="request-1",
        tenant_user_id="alice",
        session_id="session-1",
        decision=_decision(),
        outcome="success",
        latency_ms=12.5,
        namespace="default",
        forwarded_user="mnemos:opaque",
    )

    assert payload["request_id"] == "request-1"
    assert metadata["forwarded_user"] == "mnemos:opaque"
    assert metadata["forwarded_identity"] == {
        "tenant_user_id": "alice",
        "namespace": "default",
        "session_id": "session-1",
        "request_id": "request-1",
        "upstream_user": "mnemos:opaque",
    }
