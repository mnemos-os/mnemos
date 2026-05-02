"""PANTHEON OpenAI-compatible facade routes."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.core.config import get_settings
from mnemos.core.rate_limit import limiter
from mnemos.domain.pantheon import catalog, gateway, router as pantheon_router
from mnemos.domain.pantheon.aliases import PantheonRoutingError
from mnemos.domain.pantheon.caps import ConsultationCapResult, consultation_cap_bucket
from mnemos.domain.pantheon.routing_log import routing_payload, schedule_routing_memory

router = APIRouter(prefix="/pantheon/v1", tags=["pantheon"])


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
    if not get_settings().pantheon.enabled:
        raise HTTPException(status_code=503, detail="PANTHEON disabled in this profile")


def _body_model(body: dict[str, Any]) -> str:
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(status_code=400, detail="model is required")
    return model.strip()


def _to_http_exception(exc: PantheonRoutingError | gateway.PantheonGatewayError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


def _pantheon_session_id(request: Request, user: UserContext) -> str:
    return str(
        request.headers.get("x-pantheon-session")
        or getattr(user, "session_id", None)
        or getattr(request.state, "mnemos_session_id", None)
        or request.headers.get("x-mnemos-session-id")
        or request.headers.get("x-session-id")
        or request.query_params.get("session_id")
        or "default"
    )


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or f"pantheon-{uuid.uuid4()}"


def _consultation_cap_exceeded(result: ConsultationCapResult) -> JSONResponse:
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
    decision: pantheon_router.RouteDecision,
    *,
    user_id: str,
    session_id: str,
) -> ConsultationCapResult | None:
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
    request_id: str,
    tenant_user_id: str,
    session_id: str,
    decision: pantheon_router.RouteDecision,
    outcome: str,
    started_at: float,
    response: dict[str, Any] | None = None,
    error_class: str | None = None,
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
    )
    schedule_routing_memory(payload, metadata)


@router.get("/models")
@limiter.limit("60/minute", key_func=_pantheon_rate_key)
async def list_models(
    request: Request,
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
) -> dict[str, Any]:
    _require_enabled()
    return await catalog.models_response()


@router.post("/chat/completions")
@limiter.limit("60/minute", key_func=_pantheon_rate_key)
async def chat_completions(
    request: Request,
    body: dict[str, Any] = Body(...),
    authorization: str | None = Header(None),
    user: UserContext = Depends(_pantheon_user),
):
    _require_enabled()
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise HTTPException(status_code=400, detail="messages required")
    model = _body_model(body)
    decision: pantheon_router.RouteDecision | None = None
    session_id = _pantheon_session_id(request, user)
    request_id = _request_id(request)
    try:
        decision = await pantheon_router.route_model(model, body)
        cap_result = _check_consultation_cap(
            decision,
            user_id=user.user_id,
            session_id=session_id,
        )
        if cap_result is not None and not cap_result.allowed:
            return _consultation_cap_exceeded(cap_result)
        started_at = time.perf_counter()
        if body.get("stream") is True:
            _log_route_outcome(
                request_id=request_id,
                tenant_user_id=user.user_id,
                session_id=session_id,
                decision=decision,
                outcome="success",
                started_at=started_at,
            )
            return StreamingResponse(
                gateway.stream_chat_completion(decision, body),
                media_type="text/event-stream",
            )
        response_data = await gateway.forward_chat_completion(decision, body)
        _log_route_outcome(
            request_id=request_id,
            tenant_user_id=user.user_id,
            session_id=session_id,
            decision=decision,
            outcome="success",
            started_at=started_at,
            response=response_data,
        )
        return JSONResponse(response_data)
    except PantheonRoutingError as exc:
        raise _to_http_exception(exc) from exc
    except gateway.PantheonGatewayError as exc:
        if decision is not None:
            _log_route_outcome(
                request_id=request_id,
                tenant_user_id=user.user_id,
                session_id=session_id,
                decision=decision,
                outcome="error",
                started_at=started_at,
                error_class=exc.__class__.__name__,
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
    _require_enabled()
    if "input" not in body:
        raise HTTPException(status_code=400, detail="input is required")
    model = _body_model(body)
    decision: pantheon_router.RouteDecision | None = None
    session_id = _pantheon_session_id(request, user)
    request_id = _request_id(request)
    try:
        decision = await pantheon_router.route_model(model, body)
        started_at = time.perf_counter()
        response_data = await gateway.forward_embeddings(decision, body)
        _log_route_outcome(
            request_id=request_id,
            tenant_user_id=user.user_id,
            session_id=session_id,
            decision=decision,
            outcome="success",
            started_at=started_at,
            response=response_data,
        )
        return JSONResponse(response_data)
    except PantheonRoutingError as exc:
        raise _to_http_exception(exc) from exc
    except gateway.PantheonGatewayError as exc:
        if decision is not None:
            _log_route_outcome(
                request_id=request_id,
                tenant_user_id=user.user_id,
                session_id=session_id,
                decision=decision,
                outcome="error",
                started_at=started_at,
                error_class=exc.__class__.__name__,
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
    _require_enabled()
    request_body: dict[str, Any] = dict(body or {})
    if model_or_alias is not None:
        request_body["model_or_alias"] = model_or_alias
    if model is not None:
        request_body["model"] = model
    try:
        return await pantheon_router.explain_route(request_body)
    except PantheonRoutingError as exc:
        raise _to_http_exception(exc) from exc
