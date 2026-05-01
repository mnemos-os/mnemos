"""Request-ID middleware + log-correlation contract tests (v3.2).

Covers:
  * Middleware generates an ID when none is sent.
  * Middleware honors a sane inbound X-Request-ID.
  * Middleware REJECTS a hostile inbound ID and falls back to a fresh one
    (too long, control chars, empty).
  * Response echoes the ID in X-Request-ID.
  * `current_request_id()` returns the bound ID inside a handler AND
    returns None outside a request.
  * `_RequestIDLogFilter` injects request_id onto log records.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mnemos.core.observability import (
    REQUEST_ID_HEADER,
    RequestIDMiddleware,
    _RequestIDLogFilter,
    current_request_id,
    install_log_correlation,
)


def _app():
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/echo-rid")
    def _echo():
        return {"request_id": current_request_id()}

    return app


# ---- ID generation / pass-through ------------------------------------------


def test_middleware_generates_id_when_none_sent():
    client = TestClient(_app())
    resp = client.get("/echo-rid")
    assert resp.status_code == 200

    rid = resp.headers.get(REQUEST_ID_HEADER)
    assert rid, "response missing X-Request-ID header"
    # Fresh UUID4 hex is 32 chars, lowercase hex
    assert len(rid) == 32
    assert all(c in "0123456789abcdef" for c in rid)

    # Handler saw the same ID via current_request_id()
    assert resp.json()["request_id"] == rid


def test_middleware_honors_sane_inbound_id():
    client = TestClient(_app())
    resp = client.get("/echo-rid", headers={REQUEST_ID_HEADER: "client-abc-123"})
    assert resp.status_code == 200
    assert resp.headers.get(REQUEST_ID_HEADER) == "client-abc-123"
    assert resp.json()["request_id"] == "client-abc-123"


def test_middleware_rejects_inbound_with_control_chars():
    """Control chars in X-Request-ID could smuggle newlines into logs
    (log-injection attack). The middleware must discard such values
    and generate a fresh ID."""
    hostile = "abc\r\n[INJECTED]"
    client = TestClient(_app())
    resp = client.get("/echo-rid", headers={REQUEST_ID_HEADER: hostile})
    assert resp.status_code == 200
    echoed = resp.headers.get(REQUEST_ID_HEADER)
    assert echoed != hostile
    assert len(echoed) == 32  # fresh UUID4 hex


def test_middleware_rejects_overlong_inbound():
    """An absurdly long inbound ID inflates every log line for the
    request's lifetime. The middleware caps at 128 chars; anything
    longer is discarded."""
    too_long = "a" * 500
    client = TestClient(_app())
    resp = client.get("/echo-rid", headers={REQUEST_ID_HEADER: too_long})
    assert resp.status_code == 200
    echoed = resp.headers.get(REQUEST_ID_HEADER)
    assert echoed != too_long
    assert len(echoed) == 32


def test_middleware_rejects_empty_inbound():
    client = TestClient(_app())
    resp = client.get("/echo-rid", headers={REQUEST_ID_HEADER: ""})
    assert resp.status_code == 200
    assert len(resp.headers[REQUEST_ID_HEADER]) == 32


def test_middleware_each_request_gets_distinct_id():
    client = TestClient(_app())
    a = client.get("/echo-rid").headers[REQUEST_ID_HEADER]
    b = client.get("/echo-rid").headers[REQUEST_ID_HEADER]
    assert a != b


# ---- context-var behavior outside a request --------------------------------


def test_current_request_id_is_none_outside_request():
    # No middleware has run — context is bare.
    assert current_request_id() is None


# ---- log-correlation filter -------------------------------------------------


def test_log_filter_attaches_request_id_to_records():
    """Outside a request, filter stamps '-'. Inside (via ContextVar
    manipulation), filter stamps the bound value."""
    filt = _RequestIDLogFilter()

    record_out = logging.LogRecord(
        name="x", level=logging.INFO, pathname="", lineno=0,
        msg="outside", args=(), exc_info=None,
    )
    assert filt.filter(record_out) is True
    assert record_out.request_id == "-"

    from mnemos.core.observability import _request_id_ctx
    token = _request_id_ctx.set("bound-id-xyz")
    try:
        record_in = logging.LogRecord(
            name="x", level=logging.INFO, pathname="", lineno=0,
            msg="inside", args=(), exc_info=None,
        )
        assert filt.filter(record_in) is True
        assert record_in.request_id == "bound-id-xyz"
    finally:
        _request_id_ctx.reset(token)


def test_install_log_correlation_is_idempotent():
    """Calling install_log_correlation twice (e.g. on dev-server
    reloads) must not double-install the filter — the resulting log
    records should have a single request_id attribute, not multiple."""
    root = logging.getLogger()
    install_log_correlation()
    install_log_correlation()
    filter_count = sum(
        1 for f in root.filters if isinstance(f, _RequestIDLogFilter)
    )
    assert filter_count == 1


# ---- handler access via current_request_id() ------------------------------


def test_handler_sees_id_via_current_request_id():
    """Handlers get the request_id via `current_request_id()`. Starlette's
    BaseHTTPMiddleware uses a distinct Request for its dispatch, so we
    deliberately don't rely on request.state — the ContextVar is the
    single source of truth."""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    captured = {}

    @app.get("/check-ctx")
    def _check():
        captured["ctx_rid"] = current_request_id()
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/check-ctx", headers={REQUEST_ID_HEADER: "ctx-test-42"})
    assert resp.status_code == 200
    assert captured["ctx_rid"] == "ctx-test-42"
    assert resp.headers[REQUEST_ID_HEADER] == "ctx-test-42"


# ---- Prometheus /metrics (v3.2 observability slice 2) ---------------------


def _metrics_app():
    """Build a minimal app with the Prometheus middleware + endpoint
    wired, mirroring api_server.py's pattern."""
    from mnemos.core.observability import PrometheusMiddleware, metrics_router

    app = FastAPI()
    app.add_middleware(PrometheusMiddleware)
    app.include_router(metrics_router)

    @app.get("/widgets/{widget_id}")
    def _get_widget(widget_id: str):
        return {"id": widget_id}

    @app.get("/fail")
    def _fail():
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="boom")

    return app


# The /metrics tests below exercise the prometheus_client exporter wire
# format. Skip the whole group when the package is absent — operators
# who run MNEMOS without prometheus_client still get a stub /metrics
# endpoint, but the integration-level assertions below need the real
# package to validate the canonical format.
pytest.importorskip("prometheus_client")


def test_metrics_endpoint_serves_prometheus_text():
    client = TestClient(_metrics_app())
    resp = client.get("/metrics")
    assert resp.status_code == 200
    ct = resp.headers["content-type"]
    # Accept either the canonical text/plain or versioned variant
    assert "text/plain" in ct


def test_metrics_counter_bumps_per_request():
    """Each request increments mnemos_http_requests_total by 1 with
    the matched route template, not the concrete URL."""
    client = TestClient(_metrics_app())
    client.get("/widgets/abc-1")
    client.get("/widgets/abc-2")

    body = client.get("/metrics").text
    # Route template is /widgets/{widget_id}, not /widgets/abc-1
    assert 'route="/widgets/{widget_id}"' in body
    assert 'route="/widgets/abc-1"' not in body
    # Two 2xx responses bumped the counter
    import re
    m = re.search(
        r'mnemos_http_requests_total\{[^}]*route="/widgets/\{widget_id\}"[^}]*\}\s+([0-9.]+)',
        body,
    )
    assert m, "expected counter line for /widgets/{widget_id}"
    assert float(m.group(1)) >= 2.0


def test_metrics_records_status_class_label():
    """5xx responses land under status='5xx' label, not '500'. Keeps
    cardinality bounded; alerting off status class is what operators
    actually want."""
    client = TestClient(_metrics_app())
    client.get("/fail", headers={}, follow_redirects=False)
    body = client.get("/metrics").text
    assert 'status="5xx"' in body


def test_metrics_histogram_present():
    """The latency histogram must appear for every served route. We
    don't assert specific bucket values (timing-dependent) — just
    that the metric family and a _count observation exist."""
    client = TestClient(_metrics_app())
    client.get("/widgets/xyz")
    body = client.get("/metrics").text
    assert "mnemos_http_request_duration_seconds" in body
    assert "mnemos_http_request_duration_seconds_count" in body


def test_metrics_unknown_route_bucketed_as_no_route():
    """404s for URLs that don't match any route go under
    route='__no_route__' rather than creating one time series per
    probed path. Protects against adversarial cardinality blow-up."""
    client = TestClient(_metrics_app())
    client.get("/does-not-exist")
    client.get("/also-fake")
    body = client.get("/metrics").text
    assert 'route="__no_route__"' in body
    assert 'route="/does-not-exist"' not in body


# ---- Metrics auth gate (MNEMOS_METRICS_REQUIRE_AUTH) -----------------------
#
# Default is OFF — operators network-scope the scrape endpoint via
# their ingress / firewall. When flipped on, /metrics requires a
# valid Bearer token from the same ``api_keys`` table the rest of
# the API uses. These tests pin both shapes so a refactor of the
# gate cannot silently regress to either always-open or always-closed.


import hashlib
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock


def _auth_app(*, require_auth: bool):
    """Same /metrics app but with metrics_require_auth toggled via
    monkeypatching the live setting through a contextmanager
    pattern. Returns the FastAPI app + a callable to install/remove
    the toggle."""
    from mnemos.core.observability import PrometheusMiddleware, metrics_router

    app = FastAPI()
    app.add_middleware(PrometheusMiddleware)
    app.include_router(metrics_router)

    # Mount a fake pool on app.state so the auth path's pool lookup
    # finds something. The pool itself is replaced per-test depending
    # on what api_keys row should be returned.
    app.state.pool = None
    return app


@contextmanager
def _override_metrics_auth(value: bool):
    """Toggle MNEMOS_METRICS_REQUIRE_AUTH at runtime by patching the
    settings instance get_settings() returns. Cleanup restores the
    original value."""
    from mnemos.core import config as config_mod

    settings = config_mod.get_settings()
    obs = settings.observability
    original = obs.metrics_require_auth
    object.__setattr__(obs, "metrics_require_auth", value)
    try:
        yield
    finally:
        object.__setattr__(obs, "metrics_require_auth", original)


def _install_auth_pool(app, *, key_hash_to_row: dict):
    """Wire a fake asyncpg pool onto app.state.pool that returns the
    given row mapping for SELECT queries against api_keys."""

    class _AsyncCtx:
        def __init__(self, conn):
            self._c = conn

        async def __aenter__(self):
            return self._c

        async def __aexit__(self, *args):
            return None

    conn = MagicMock()

    async def _fetchrow(sql, value):
        return key_hash_to_row.get(value)

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    app.state.pool = pool


def test_metrics_open_by_default(monkeypatch):
    """Default behaviour: no Authorization header required, no
    Bearer token required, /metrics returns 200."""
    app = _auth_app(require_auth=False)
    client = TestClient(app)
    with _override_metrics_auth(False):
        resp = client.get("/metrics")
    assert resp.status_code == 200


def test_metrics_open_default_ignores_invalid_bearer(monkeypatch):
    """Even with a malformed Authorization header, default-mode
    /metrics still returns 200 — the gate is off."""
    app = _auth_app(require_auth=False)
    client = TestClient(app)
    with _override_metrics_auth(False):
        resp = client.get("/metrics", headers={"Authorization": "Bearer bogus"})
    assert resp.status_code == 200


def test_metrics_auth_required_rejects_missing_token(monkeypatch):
    """When the gate is on and no Authorization header is sent,
    /metrics returns 401 with WWW-Authenticate: Bearer."""
    app = _auth_app(require_auth=True)
    client = TestClient(app)
    with _override_metrics_auth(True):
        resp = client.get("/metrics")
    assert resp.status_code == 401
    assert "Bearer" in resp.headers.get("www-authenticate", "")


def test_metrics_auth_required_rejects_non_bearer_scheme(monkeypatch):
    """A Basic or other-scheme header doesn't satisfy the Bearer
    requirement."""
    app = _auth_app(require_auth=True)
    client = TestClient(app)
    with _override_metrics_auth(True):
        resp = client.get(
            "/metrics", headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
    assert resp.status_code == 401


def test_metrics_auth_required_rejects_empty_token(monkeypatch):
    app = _auth_app(require_auth=True)
    client = TestClient(app)
    with _override_metrics_auth(True):
        resp = client.get("/metrics", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401


def test_metrics_auth_required_503_when_no_pool(monkeypatch):
    """The auth path needs the DB pool to look up the token's hash.
    With auth required AND no pool, fail closed (503), do NOT silently
    serve the metrics body."""
    app = _auth_app(require_auth=True)
    app.state.pool = None
    client = TestClient(app)
    with _override_metrics_auth(True):
        resp = client.get(
            "/metrics", headers={"Authorization": "Bearer sometoken"},
        )
    assert resp.status_code == 503


def test_metrics_auth_required_rejects_unknown_token(monkeypatch):
    app = _auth_app(require_auth=True)
    _install_auth_pool(app, key_hash_to_row={})
    client = TestClient(app)
    with _override_metrics_auth(True):
        resp = client.get(
            "/metrics", headers={"Authorization": "Bearer wrong-token"},
        )
    assert resp.status_code == 401


def test_metrics_auth_required_rejects_revoked_token(monkeypatch):
    raw_token = "revoked-token-xyz"
    key_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    app = _auth_app(require_auth=True)
    _install_auth_pool(app, key_hash_to_row={key_hash: {"revoked": True}})
    client = TestClient(app)
    with _override_metrics_auth(True):
        resp = client.get(
            "/metrics", headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert resp.status_code == 401


def test_metrics_auth_required_admits_valid_token(monkeypatch):
    """Happy path: gate on, valid Bearer token, /metrics returns 200."""
    raw_token = "good-token-abc"
    key_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    app = _auth_app(require_auth=True)
    _install_auth_pool(app, key_hash_to_row={key_hash: {"revoked": False}})
    client = TestClient(app)
    with _override_metrics_auth(True):
        resp = client.get(
            "/metrics", headers={"Authorization": f"Bearer {raw_token}"},
        )
    assert resp.status_code == 200
    # Body shape is the standard prometheus exposition.
    assert "text/plain" in resp.headers["content-type"]


# ---- OpenTelemetry tracing (v3.2 observability slice 3) --------------------


# Module-level: OTel's TracerProvider is a process global that can
# only be set ONCE per run. We install a shared provider on first use
# and reuse the same InMemorySpanExporter across all tracing tests,
# clearing its captured spans at the start of each test.
_SHARED_EXPORTER = None


def _tracing_app():
    """Build a minimal app with TracingMiddleware wired. Spans land in
    the shared in-memory exporter; caller should clear it first.
    Skips the whole test if opentelemetry isn't installed.
    """
    pytest.importorskip("opentelemetry")

    global _SHARED_EXPORTER
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    import mnemos.core.observability as obs
    from mnemos.core.observability import RequestIDMiddleware, TracingMiddleware

    if _SHARED_EXPORTER is None:
        _SHARED_EXPORTER = InMemorySpanExporter()

        # If the process already has a real TracerProvider (e.g.
        # because `import api_server` ran install_tracing() during
        # an earlier test), add our exporter TO THAT provider.
        # Otherwise install our own. Either way _SHARED_EXPORTER
        # captures every span the engine emits.
        existing = otel_trace.get_tracer_provider()
        if isinstance(existing, TracerProvider):
            existing.add_span_processor(SimpleSpanProcessor(_SHARED_EXPORTER))
        else:
            provider = TracerProvider(
                resource=Resource.create({"service.name": "test"})
            )
            provider.add_span_processor(SimpleSpanProcessor(_SHARED_EXPORTER))
            otel_trace.set_tracer_provider(provider)

        # Reset the module-level tracer cache so the next _get_tracer()
        # call pulls a tracer from the now-fully-configured provider.
        obs._tracer = None

    _SHARED_EXPORTER.clear()
    obs._tracer = None

    app = FastAPI()
    # Stacking is LIFO — add RequestID LAST so it becomes the
    # outermost middleware, guaranteeing the ContextVar is bound by
    # the time Tracing reads `current_request_id()`.
    app.add_middleware(TracingMiddleware)
    app.add_middleware(RequestIDMiddleware)

    @app.get("/widgets/{widget_id}")
    def _get_widget(widget_id: str):
        return {"id": widget_id}

    @app.get("/boom")
    def _boom():
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail="boom")

    return app, _SHARED_EXPORTER


def test_tracing_creates_span_per_request():
    app, exporter = _tracing_app()
    client = TestClient(app)
    client.get("/widgets/abc")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "GET /widgets/{widget_id}"


def test_tracing_span_attributes_include_method_route_status():
    app, exporter = _tracing_app()
    client = TestClient(app)
    client.get("/widgets/xyz")
    span = exporter.get_finished_spans()[0]
    attrs = dict(span.attributes or {})
    assert attrs["http.method"] == "GET"
    assert attrs["http.route"] == "/widgets/{widget_id}"
    assert attrs["http.status_code"] == 200


def test_tracing_span_carries_request_id():
    """Log<->trace correlation: the span attribute `mnemos.request_id`
    must match the X-Request-ID echoed back to the client so operators
    can jump from a log line to the corresponding span.
    """
    app, exporter = _tracing_app()
    client = TestClient(app)
    resp = client.get(
        "/widgets/alpha",
        headers={REQUEST_ID_HEADER: "test-rid-000"},
    )
    span_attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert span_attrs["mnemos.request_id"] == "test-rid-000"
    assert resp.headers[REQUEST_ID_HEADER] == "test-rid-000"


def test_tracing_records_5xx_status():
    """5xx responses still close the span cleanly with the correct
    status attribute — don't lose the observation on the error path."""
    app, exporter = _tracing_app()
    client = TestClient(app)
    client.get("/boom")
    span_attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert span_attrs["http.status_code"] == 500


def test_install_tracing_is_idempotent():
    """Calling install_tracing twice must not re-install the provider
    (prevents test-suite cross-contamination on dev-server reloads)."""
    pytest.importorskip("opentelemetry")
    import mnemos.core.observability as obs
    from mnemos.core.observability import install_tracing

    # Reset the install flag so we can exercise the idempotent path
    obs._TRACING_INSTALLED = False
    install_tracing()
    assert obs._TRACING_INSTALLED is True
    # Second call is a no-op
    install_tracing()
    assert obs._TRACING_INSTALLED is True


def test_tracing_middleware_passthrough_when_otel_missing(monkeypatch):
    """If opentelemetry isn't available, the middleware must pass
    requests through unchanged — never raise, never 500 a caller."""
    import mnemos.core.observability as obs

    # Simulate OTel missing
    monkeypatch.setattr(obs, "_OTEL_AVAILABLE", False)

    from mnemos.core.observability import TracingMiddleware
    app = FastAPI()
    app.add_middleware(TracingMiddleware)

    @app.get("/ok")
    def _ok():
        return {"ok": True}

    client = TestClient(app)
    resp = client.get("/ok")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ---- structlog JSON rendering (v3.2 observability slice 4) ----------------


def test_install_structured_logging_is_idempotent():
    """Calling install_structured_logging twice is a no-op on the
    second invocation. Prevents double-install when a dev-server
    reloads and re-imports the module."""
    pytest.importorskip("structlog")
    import mnemos.core.observability as obs
    from mnemos.core.observability import install_structured_logging

    obs._STRUCTLOG_INSTALLED = False  # reset so first call does work
    install_structured_logging()
    assert obs._STRUCTLOG_INSTALLED is True
    # Second call no-ops
    install_structured_logging()
    assert obs._STRUCTLOG_INSTALLED is True


def test_install_structured_logging_noop_when_structlog_missing(monkeypatch):
    """If structlog isn't installed, install_structured_logging
    doesn't raise — it logs + returns. Matches the soft-optional
    contract for OTel and prometheus_client."""
    import mnemos.core.observability as obs

    monkeypatch.setattr(obs, "_STRUCTLOG_AVAILABLE", False)
    obs._STRUCTLOG_INSTALLED = False

    # Must not raise
    obs.install_structured_logging()
    assert obs._STRUCTLOG_INSTALLED is True


def test_structured_logging_renders_json_with_request_id(caplog):
    """When structured logging is active AND a request_id is bound,
    the emitted log line carries that request_id in the event dict.
    We read the formatter's output via caplog — if it's valid JSON
    and contains request_id + event, the pipeline is wired."""
    pytest.importorskip("structlog")

    import json as _json
    import logging as _logging

    import mnemos.core.observability as obs
    from mnemos.core.observability import (
        _request_id_ctx,
        install_structured_logging,
    )

    # Force a clean install
    obs._STRUCTLOG_INSTALLED = False
    install_structured_logging()

    root = _logging.getLogger()
    # Find a handler with the structlog formatter to capture its
    # output. We add a dedicated StreamHandler with the same formatter
    # and a StringIO buffer so we can assert on the rendered text.
    import io

    buf = io.StringIO()
    capture_handler = _logging.StreamHandler(buf)
    # The formatter installed on existing handlers is the structlog
    # ProcessorFormatter; reuse one from an existing root handler.
    existing_fmt = next(
        (h.formatter for h in root.handlers if h.formatter is not None),
        None,
    )
    if existing_fmt is None:
        pytest.skip("no formatter attached to root — structlog setup raced")
    capture_handler.setFormatter(existing_fmt)
    capture_handler.setLevel(_logging.INFO)
    root.addHandler(capture_handler)

    try:
        token = _request_id_ctx.set("struct-rid-abc")
        try:
            test_logger = _logging.getLogger("mnemos.test.struct")
            test_logger.setLevel(_logging.INFO)
            # Ensure root level permits INFO in case basicConfig
            # locked it at WARNING from a prior test.
            root.setLevel(_logging.INFO)
            test_logger.info("hello structured world")
            capture_handler.flush()
        finally:
            _request_id_ctx.reset(token)
    finally:
        root.removeHandler(capture_handler)

    output = buf.getvalue().strip()
    # Output is JSON. Parse and assert the expected fields.
    # structlog ProcessorFormatter may emit trailing newline per record.
    for line in output.splitlines():
        if not line.strip():
            continue
        parsed = _json.loads(line)
        if parsed.get("event") == "hello structured world":
            assert parsed["request_id"] == "struct-rid-abc"
            assert "timestamp" in parsed
            assert parsed.get("level", "").lower() == "info"
            break
    else:
        pytest.fail(f"expected log event not found in rendered output: {output!r}")
