"""PANTHEON OpenAI-compatible facade routes."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.core.config import get_settings
from mnemos.core.extras import is_extra_installed, missing_extra_detail
from mnemos.core.rate_limit import limiter

router = APIRouter(prefix="/pantheon/v1", tags=["pantheon"])
logger = logging.getLogger(__name__)


async def _pantheon_user(
    request: Request,
    user: UserContext = Depends(get_current_user),
) -> UserContext:
    request.state.mnemos_pantheon_user_id = user.user_id
    return user


def _pantheon_rate_key(request: Request) -> str:
    user_id = getattr(request.state, "mnemos_pantheon_user_id", None)
    session_id = (
        request.headers.get("x-pantheon-session")
        or request.headers.get("x-mnemos-session-id")
        or request.headers.get("x-session-id")
        or request.query_params.get("session_id")
        or "default"
    )
    if user_id:
        return f"pantheon:{user_id}:{session_id}"
    client = request.client.host if request.client else "unknown"
    return f"pantheon:{client}:{session_id}"


def _require_enabled() -> None:
    if not is_extra_installed("pantheon"):
        raise HTTPException(
            status_code=503,
            detail=missing_extra_detail("pantheon", label="PANTHEON"),
        )
    if not get_settings().pantheon.enabled:
        raise HTTPException(status_code=503, detail="PANTHEON disabled in this profile")


def _pantheon_imports() -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    _require_enabled()
    from mnemos.domain.pantheon import catalog, gateway, router as pantheon_router
    from mnemos.domain.pantheon.aliases import PantheonRoutingError
    from mnemos.domain.pantheon.caps import consultation_cap_bucket
    from mnemos.domain.pantheon.routing_log import routing_payload, schedule_routing_memory

    return (
        catalog,
        gateway,
        pantheon_router,
        PantheonRoutingError,
        consultation_cap_bucket,
        routing_payload,
        schedule_routing_memory,
    )


def _body_model(body: dict[str, Any]) -> str:
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(status_code=400, detail="model is required")
    return model.strip()


def _to_http_exception(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=getattr(exc, "status_code", 500),
        detail=getattr(exc, "message", str(exc)),
    )


def _pantheon_session_id(request: Request, user: UserContext) -> str:
    return str(
        getattr(user, "session_id", None)
        or request.headers.get("x-pantheon-session")
        or getattr(request.state, "mnemos_session_id", None)
        or request.headers.get("x-mnemos-session-id")
        or request.headers.get("x-session-id")
        or request.query_params.get("session_id")
        or "default"
    )


def _request_id(request: Request) -> str:
    return str(uuid.uuid4())


def _upstream_identity(
    gateway_module: Any,
    request: Request,
    user: UserContext,
    *,
    session_id: str,
    request_id: str,
) -> Any:
    identity = gateway_module.UpstreamIdentity(
        user_id=user.user_id,
        namespace=user.namespace,
        session_id=session_id,
        request_id=request_id,
    )
    expected = {
        "x-mnemos-user-id": identity.user_id,
        "x-mnemos-namespace": identity.namespace,
        "x-mnemos-session": identity.session_id,
        "x-mnemos-request-id": identity.request_id,
    }
    for header, value in expected.items():
        supplied = request.headers.get(header)
        if supplied is not None and supplied != value:
            logger.warning(
                "[PANTHEON] stripped spoofed %s header for request_id=%s",
                header,
                request_id,
            )
    return identity


def _consultation_cap_exceeded(result: Any) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": "0"},
        content={
            "error": {
                "type": "pantheon_usage_tier_cap_exceeded",
                "message": (
                    "usage_tier=consultation_only is capped per user session; "
                    "start a new session or choose an agentic_ok model for agent workflows"
                ),
                "usage_tier": "consultation_only",
                "cap": result.cap,
                "used": result.used,
                "retry_after": None,
            }
        },
    )


def _check_consultation_cap(
    consultation_cap_bucket: Any,
    decision: Any,
    *,
    user_id: str,
    session_id: str,
) -> Any | None:
    model = decision.model or {}
    if model.get("usage_tier") != "consultation_only":
        return None
    cap = get_settings().pantheon.consultation_cap
    return consultation_cap_bucket.check_and_increment(
        user_id=user_id,
        session_id=session_id,
        cap=cap,
    )


def _log_route_outcome(
    *,
    routing_payload: Any,
    schedule_routing_memory: Any,
    request_id: str,
    tenant_user_id: str,
    session_id: str,
    decision: Any,
    outcome: str,
    started_at: float,
    response: dict[str, Any] | None = None,
    error_class: str | None = None,
    namespace: str | None = None,
    forwarded_user: str | None = None,
) -> None:
    payload, metadata = routing_payload(
        request_id=request_id,
        tenant_user_id=tenant_user_id,
        session_id=session_id,
        decision=decision,
        outcome=outcome,
        latency_ms=round((time.perf_counter() - started_at) * 1000.0, 3),
        response=response,
        error_class=error_class,
        namespace=namespace,
        forwarded_user=forwarded_user,
    )
    schedule_routing_memory(payload, metadata)


@router.get("/models")
@limiter.limit("60/minute", key_func=_pantheon_rate_key)
async def list_models(
    request: Request,
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
) -> dict[str, Any]:
    catalog, *_ = _pantheon_imports()
    return await catalog.models_response()


@router.post("/chat/completions")
@limiter.limit("60/minute", key_func=_pantheon_rate_key)
async def chat_completions(
    request: Request,
    body: dict[str, Any] = Body(...),
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
):
    (
        _catalog,
        gateway,
        pantheon_router,
        PantheonRoutingError,
        consultation_cap_bucket,
        routing_payload,
        schedule_routing_memory,
    ) = _pantheon_imports()
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise HTTPException(status_code=400, detail="messages required")
    model = _body_model(body)
    decision: Any | None = None
    session_id = _pantheon_session_id(request, user)
    request_id = _request_id(request)
    identity = _upstream_identity(gateway, request, user, session_id=session_id, request_id=request_id)
    try:
        decision = await pantheon_router.route_model(model, body)
        cap_result = _check_consultation_cap(
            consultation_cap_bucket,
            decision,
            user_id=user.user_id,
            session_id=session_id,
        )
        if cap_result is not None and not cap_result.allowed:
            return _consultation_cap_exceeded(cap_result)
        started_at = time.perf_counter()
        if body.get("stream") is True:
            forward_body = gateway.attach_upstream_identity(body, identity)
            _log_route_outcome(
                routing_payload=routing_payload,
                schedule_routing_memory=schedule_routing_memory,
                request_id=request_id,
                tenant_user_id=user.user_id,
                session_id=session_id,
                decision=decision,
                outcome="success",
                started_at=started_at,
                namespace=user.namespace,
                forwarded_user=identity.opaque_user,
            )
            return StreamingResponse(
                gateway.stream_chat_completion(decision, forward_body),
                media_type="text/event-stream",
            )
        forward_body = gateway.attach_upstream_identity(body, identity)
        response_data = await gateway.forward_chat_completion(decision, forward_body)
        _log_route_outcome(
            routing_payload=routing_payload,
            schedule_routing_memory=schedule_routing_memory,
            request_id=request_id,
            tenant_user_id=user.user_id,
            session_id=session_id,
            decision=decision,
            outcome="success",
            started_at=started_at,
            response=response_data,
            namespace=user.namespace,
            forwarded_user=identity.opaque_user,
        )
        return JSONResponse(response_data)
    except PantheonRoutingError as exc:
        raise _to_http_exception(exc) from exc
    except gateway.PantheonGatewayError as exc:
        if decision is not None:
            _log_route_outcome(
                routing_payload=routing_payload,
                schedule_routing_memory=schedule_routing_memory,
                request_id=request_id,
                tenant_user_id=user.user_id,
                session_id=session_id,
                decision=decision,
                outcome="error",
                started_at=started_at,
                error_class=exc.__class__.__name__,
                namespace=user.namespace,
                forwarded_user=identity.opaque_user,
            )
        raise _to_http_exception(exc) from exc


@router.post("/embeddings")
@limiter.limit("60/minute", key_func=_pantheon_rate_key)
async def embeddings(
    request: Request,
    body: dict[str, Any] = Body(...),
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
) -> JSONResponse:
    (
        _catalog,
        gateway,
        pantheon_router,
        PantheonRoutingError,
        _consultation_cap_bucket,
        routing_payload,
        schedule_routing_memory,
    ) = _pantheon_imports()
    if "input" not in body:
        raise HTTPException(status_code=400, detail="input is required")
    model = _body_model(body)
    decision: Any | None = None
    session_id = _pantheon_session_id(request, user)
    request_id = _request_id(request)
    identity = _upstream_identity(gateway, request, user, session_id=session_id, request_id=request_id)
    try:
        decision = await pantheon_router.route_model(model, body)
        started_at = time.perf_counter()
        forward_body = gateway.attach_upstream_identity(body, identity)
        response_data = await gateway.forward_embeddings(decision, forward_body)
        _log_route_outcome(
            routing_payload=routing_payload,
            schedule_routing_memory=schedule_routing_memory,
            request_id=request_id,
            tenant_user_id=user.user_id,
            session_id=session_id,
            decision=decision,
            outcome="success",
            started_at=started_at,
            response=response_data,
            namespace=user.namespace,
            forwarded_user=identity.opaque_user,
        )
        return JSONResponse(response_data)
    except PantheonRoutingError as exc:
        raise _to_http_exception(exc) from exc
    except gateway.PantheonGatewayError as exc:
        if decision is not None:
            _log_route_outcome(
                routing_payload=routing_payload,
                schedule_routing_memory=schedule_routing_memory,
                request_id=request_id,
                tenant_user_id=user.user_id,
                session_id=session_id,
                decision=decision,
                outcome="error",
                started_at=started_at,
                error_class=exc.__class__.__name__,
                namespace=user.namespace,
                forwarded_user=identity.opaque_user,
            )
        raise _to_http_exception(exc) from exc


@router.get("/route/explain")
@limiter.limit("60/minute", key_func=_pantheon_rate_key)
async def route_explain(
    request: Request,
    body: dict[str, Any] | None = Body(default=None),
    model: str | None = Query(default=None),
    model_or_alias: str | None = Query(default=None),
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
) -> dict[str, Any]:
    (
        _catalog,
        _gateway,
        pantheon_router,
        PantheonRoutingError,
        _consultation_cap_bucket,
        _routing_payload,
        _schedule_routing_memory,
    ) = _pantheon_imports()
    request_body: dict[str, Any] = dict(body or {})
    if model_or_alias is not None:
        request_body["model_or_alias"] = model_or_alias
    if model is not None:
        request_body["model"] = model
    try:
        return await pantheon_router.explain_route(request_body)
    except PantheonRoutingError as exc:
        raise _to_http_exception(exc) from exc
