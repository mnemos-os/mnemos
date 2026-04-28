"""Request-ID middleware + log correlation (v3.2 observability).

Assigns a unique ID to every incoming request, binds it to a
ContextVar for the request's lifetime, emits it in the X-Request-ID
response header, and injects it into every log record produced during
the request via a logging.Filter.

Foundation commit: Prometheus /metrics, OpenTelemetry tracing, and
structlog integration follow in their own commits and reuse the
`current_request_id()` accessor defined here.

## Usage

    from mnemos.core.observability import RequestIDMiddleware, current_request_id

    app.add_middleware(RequestIDMiddleware)

Any code path reached during a request can call
`current_request_id()` to get the correlating UUID. Outside a request
context, it returns None.

## Callers bringing their own correlation ID

If the inbound request carries an `X-Request-ID` header, we honor it
verbatim so correlation survives across a load balancer / gateway
hop. Validation is minimal — we strip to 128 ASCII printables and
cap length at 128 chars, defensive against an adversary trying to
inject control characters or blow up log lines. If the inbound header
is absent or fails validation, we generate a fresh UUID4 hex.
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from contextvars import ContextVar
from typing import Awaitable, Callable, Optional

from fastapi import APIRouter
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)
from starlette.requests import Request
from starlette.responses import Response

try:  # Soft-optional — if prometheus_client isn't installed, the
      # /metrics endpoint serves a tiny stub and the middleware no-ops.
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Histogram,
        generate_latest,
    )
    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover — dev environments that skip the dep
    _PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"

try:  # Soft-optional — if opentelemetry isn't installed, tracing is a no-op.
    from opentelemetry import trace as _otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    _OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OTEL_AVAILABLE = False
    _otel_trace = None
    Resource = None
    TracerProvider = None
    BatchSpanProcessor = None

try:  # Soft-optional — if structlog isn't installed, install_structured_logging
      # is a no-op and logs stay in the human-readable format.
    import structlog
    _STRUCTLOG_AVAILABLE = True
except ImportError:  # pragma: no cover
    _STRUCTLOG_AVAILABLE = False
    structlog = None


# The header name operators see and can override from upstream proxies.
REQUEST_ID_HEADER = "X-Request-ID"

# Max length we accept for an incoming correlation ID. Anything longer
# is discarded and replaced with a fresh UUID — keeps log lines
# bounded even when a caller passes an absurdly long header.
_MAX_INBOUND_LENGTH = 128

# Only ASCII printables (no whitespace / no control chars). Prevents
# an adversary from smuggling newlines into logs via X-Request-ID.
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


# ContextVar is task-local — asyncio tasks spawned from within a
# request inherit the bound value. Good enough for FastAPI's default
# "per-request is one task" shape.
_request_id_ctx: ContextVar[Optional[str]] = ContextVar("mnemos_request_id", default=None)


def current_request_id() -> Optional[str]:
    """Return the request_id bound to the current async context, or
    None if we're outside a request (e.g. module import, background
    worker tick)."""
    return _request_id_ctx.get()


def _validate_inbound(value: str) -> Optional[str]:
    """Accept the inbound ID only if it's reasonably shaped.
    Returns the sanitized value or None to indicate "generate fresh"."""
    if not value:
        return None
    value = value.strip()
    if not value or len(value) > _MAX_INBOUND_LENGTH:
        return None
    if not _SAFE_ID_RE.match(value):
        return None
    return value


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that binds a request ID for the lifetime
    of every HTTP request.

    Preferred ID source order:
      1. Inbound X-Request-ID header (validated)
      2. Fresh uuid4().hex

    The bound value is:
      * stored in the `_request_id_ctx` ContextVar — visible to any
        code in the request's task tree via `current_request_id()`
      * echoed to the client in the X-Request-ID response header
      * picked up by `_RequestIDLogFilter` for every log record
        produced during the request

    We intentionally do NOT stash the ID on request.state. Starlette's
    BaseHTTPMiddleware creates a distinct Request object for its
    dispatch, so a state setter here would not propagate to the
    handler's Request. The ContextVar is the single source of truth;
    handlers that need the ID should call `current_request_id()`.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        inbound = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = _validate_inbound(inbound) or uuid.uuid4().hex

        token = _request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
        finally:
            _request_id_ctx.reset(token)

        # Echo the ID so the client can cite it when reporting an issue.
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class _RequestIDLogFilter(logging.Filter):
    """Attaches the current request_id (or '-' outside a request) to
    every log record as `record.request_id`. Configure the root
    formatter to print `%(request_id)s` to surface it in log lines.

    Attached by `install_log_correlation()` to the root logger — every
    handler already in place picks up the filter automatically
    because logging.Filter walks up the hierarchy.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id() or "-"
        return True


def install_log_correlation(
    fmt: Optional[str] = None,
    *,
    replace_root_formatter: bool = True,
) -> None:
    """Attach `_RequestIDLogFilter` to the root logger and optionally
    replace the root handler's formatter with one that prints
    request_id. Call once at startup, before any handlers fire.

    `fmt` overrides the default format string.
    """
    if fmt is None:
        fmt = (
            "%(asctime)s [%(levelname)s] [req:%(request_id)s] "
            "%(name)s: %(message)s"
        )

    root = logging.getLogger()
    filt = _RequestIDLogFilter()
    # Only install once — idempotent on repeated calls (dev-server reloads).
    if not any(isinstance(f, _RequestIDLogFilter) for f in root.filters):
        root.addFilter(filt)

    if replace_root_formatter:
        formatter = logging.Formatter(fmt)
        for handler in root.handlers:
            handler.addFilter(filt)
            handler.setFormatter(formatter)


# ─── Prometheus metrics (v3.2 observability slice 2) ────────────────────────

# Default histogram buckets, optimized for web-request latency. The
# prometheus_client default buckets are fine but stretch into the
# many-second range; MNEMOS hot paths at p99 are in the 10ms-3s band,
# so we compress to get better resolution where it matters.
_LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75,
    1.0, 2.5, 5.0, 7.5, 10.0, 30.0,
)

if _PROMETHEUS_AVAILABLE:
    HTTP_REQUESTS_TOTAL = Counter(
        "mnemos_http_requests_total",
        "HTTP requests served, by method + route template + status class.",
        ["method", "route", "status"],
    )
    HTTP_REQUEST_DURATION_SECONDS = Histogram(
        "mnemos_http_request_duration_seconds",
        "Wall-clock time spent producing an HTTP response, by route.",
        ["method", "route"],
        buckets=_LATENCY_BUCKETS,
    )
else:  # pragma: no cover
    HTTP_REQUESTS_TOTAL = None
    HTTP_REQUEST_DURATION_SECONDS = None


def _route_template(request: Request) -> str:
    """Extract the matched route's path template, not the concrete
    URL. `/v1/memories/mem_abc123` -> `/v1/memories/{memory_id}`.

    Using the template as the `route` label bounds cardinality — one
    time series per endpoint, not one per memory_id. If no route
    matched (404), returns '__no_route__' so these show up as a
    single bucket rather than polluting the metric surface with every
    misspelled URL an adversary might probe.
    """
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return route.path
    return "__no_route__"


class PrometheusMiddleware(BaseHTTPMiddleware):
    """Timing + counter middleware. Records one observation per request
    on `mnemos_http_request_duration_seconds` and bumps
    `mnemos_http_requests_total` with a status-class label (2xx/3xx/
    4xx/5xx) instead of raw status code — the raw code would explode
    cardinality and is less useful for alerting than the class.

    Runs inside RequestIDMiddleware so metric exemplars (added in a
    follow-up) can attach the request_id.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not _PROMETHEUS_AVAILABLE:
            return await call_next(request)

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = _route_template(request)
            method = request.method
            status_class = f"{status_code // 100}xx"
            HTTP_REQUESTS_TOTAL.labels(
                method=method, route=route, status=status_class,
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(
                method=method, route=route,
            ).observe(elapsed)


# The /metrics router is defined here so operators importing this
# module get one place to hook everything. api_server.py includes
# this router alongside the others; no auth on /metrics, per the
# Prometheus scrape convention — operators network-scope the
# endpoint via their ingress/firewall, not per-request auth.
metrics_router = APIRouter(tags=["observability"])


@metrics_router.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus text-exposition endpoint. Returns the default-registry
    payload — every metric defined against the default registry shows
    up here.

    Returns a stub when prometheus_client isn't installed, so scrapers
    pointed at this endpoint see a clear empty-but-valid response
    instead of a 500.
    """
    if not _PROMETHEUS_AVAILABLE:
        return Response(
            content="# prometheus_client not installed\n",
            media_type=CONTENT_TYPE_LATEST,
        )
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ─── OpenTelemetry tracing (v3.2 observability slice 3) ────────────────────

_TRACING_INSTALLED: bool = False


def install_tracing(service_name: str = "mnemos") -> None:
    """Install the global OpenTelemetry TracerProvider.

    Idempotent — repeat calls are a no-op. Export target is chosen from
    standard OTel env vars:

      OTEL_EXPORTER_OTLP_ENDPOINT   — OTLP/HTTP URL. When set, spans
                                      are batched and shipped there.
      OTEL_SERVICE_NAME             — overrides the default "mnemos".

    When no endpoint is set, the TracerProvider is still installed
    so code using `_get_tracer()` records spans (useful for tests
    and for any in-process consumers) — they just aren't exported.

    When `opentelemetry` isn't installed at all, this is a log-and-
    return no-op; the middleware short-circuits too.
    """
    global _TRACING_INSTALLED

    if _TRACING_INSTALLED:
        return

    if not _OTEL_AVAILABLE:
        logger.info(
            "[observability] opentelemetry not installed; tracing is a no-op"
        )
        _TRACING_INSTALLED = True
        return

    effective_name = os.getenv("OTEL_SERVICE_NAME", service_name)
    resource = Resource.create({"service.name": effective_name})
    provider = TracerProvider(resource=resource)

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        try:  # Exporter-proto-http is also soft-optional — a leaner
              # operator may install only the SDK and export some
              # other way (file, in-memory for tests).
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
            )
            logger.info(
                "[observability] OTLP span exporter configured for %s",
                endpoint,
            )
        except ImportError:
            logger.warning(
                "[observability] OTEL_EXPORTER_OTLP_ENDPOINT set to %s but "
                "opentelemetry-exporter-otlp-proto-http isn't installed; "
                "spans will be recorded but not exported",
                endpoint,
            )

    _otel_trace.set_tracer_provider(provider)
    _TRACING_INSTALLED = True


_tracer = None


def _get_tracer():
    """Lazy tracer accessor. Uses the global TracerProvider — call
    install_tracing() at startup to bind it."""
    global _tracer
    if _tracer is None and _OTEL_AVAILABLE:
        _tracer = _otel_trace.get_tracer("mnemos.api")
    return _tracer


class TracingMiddleware(BaseHTTPMiddleware):
    """Wraps each HTTP request in an OTel span.

    Attributes set:
      http.method         — request method
      http.route          — Starlette-matched route template (same
                            shape as the Prometheus `route` label;
                            bounds cardinality)
      http.status_code    — response status (integer)
      mnemos.request_id   — the request-ID bound by RequestIDMiddleware
                            (enables log<->trace correlation)

    Placed INSIDE RequestIDMiddleware in the stack so
    `current_request_id()` is already populated when the span starts.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not _OTEL_AVAILABLE:
            return await call_next(request)

        tracer = _get_tracer()
        if tracer is None:
            return await call_next(request)

        # Starlette matches the route INSIDE its router, which runs
        # after middleware dispatch starts. We open the span with a
        # placeholder name keyed on method and refine it in finally
        # once the router has populated request.scope["route"]. This
        # mirrors the finally-based pattern in PrometheusMiddleware.
        method = request.method

        with tracer.start_as_current_span(method) as span:
            span.set_attribute("http.method", method)
            rid = current_request_id()
            if rid:
                span.set_attribute("mnemos.request_id", rid)

            status_code = 500
            try:
                response = await call_next(request)
                status_code = response.status_code
                return response
            finally:
                route = _route_template(request)
                span.update_name(f"{method} {route}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.status_code", status_code)


# ─── structlog JSON rendering (v3.2 observability slice 4) ────────────────

_STRUCTLOG_INSTALLED: bool = False


def _structlog_add_request_id(_logger, _method, event_dict):
    """structlog processor: inject the bound request_id onto every
    record. Runs after standard logging's LoggerAdapter so
    request_id is already set by _RequestIDLogFilter, but the
    processor also handles the case where structlog is used
    directly (not via standard logging) by reading from our
    ContextVar."""
    if "request_id" not in event_dict:
        rid = current_request_id()
        if rid:
            event_dict["request_id"] = rid
        else:
            event_dict["request_id"] = "-"
    return event_dict


def install_structured_logging(
    *,
    stream: Optional[object] = None,
) -> None:
    """Switch log rendering to JSON via structlog.

    No-op when `structlog` isn't installed. When it is, this:

      * Configures structlog's render chain to emit JSON.
      * Replaces every root handler's formatter with one that
        delegates to structlog's ProcessorFormatter, so code using
        plain `logging.getLogger(...)` ALSO produces JSON.
      * Registers `_structlog_add_request_id` as a processor so
        every event carries the request_id from our ContextVar.

    Intended call site: operators who want structured log output
    for a collector (Loki, Elasticsearch, Datadog). They import
    and invoke at startup; no env-var magic — it's an explicit opt-in.
    """
    global _STRUCTLOG_INSTALLED

    if _STRUCTLOG_INSTALLED:
        return

    if not _STRUCTLOG_AVAILABLE:
        logger.info(
            "[observability] structlog not installed; "
            "install_structured_logging is a no-op"
        )
        _STRUCTLOG_INSTALLED = True
        return

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _structlog_add_request_id,
        timestamper,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Pipe standard-logging records through structlog's formatter so
    # ordinary `logger.info(...)` call sites also render as JSON.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    root = logging.getLogger()
    for handler in root.handlers:
        handler.setFormatter(formatter)

    _STRUCTLOG_INSTALLED = True


__all__ = [
    "REQUEST_ID_HEADER",
    "RequestIDMiddleware",
    "PrometheusMiddleware",
    "TracingMiddleware",
    "current_request_id",
    "install_log_correlation",
    "install_structured_logging",
    "install_tracing",
    "metrics_router",
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION_SECONDS",
]
