"""Webhook subsystem tests — structure, signature, validation, delivery state machine.

Pure-Python where possible; integration tests that need a live DB are marked
with `pytest.mark.integration` and skipped when MNEMOS_TEST_DB isn't set.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Module import smoke tests ─────────────────────────────────────────────────


class TestWebhookModuleWiring:
    """Confirm modules exist and expose the expected surface."""

    def test_handler_imports(self):
        from mnemos.api.routes import webhooks
        assert hasattr(webhooks, "router")
        assert webhooks.router.prefix == "/v1/webhooks"

    def test_dispatcher_imports(self):
        from mnemos.webhooks import dispatcher as webhook_dispatcher
        assert hasattr(webhook_dispatcher, "dispatch")
        assert hasattr(webhook_dispatcher, "repair_worker_loop")
        assert hasattr(webhook_dispatcher, "delivery_worker_loop")
        assert hasattr(webhook_dispatcher, "_sign")

    def test_models_imported(self):
        from mnemos.domain.models import (
            VALID_WEBHOOK_EVENTS,
        )
        assert {"memory.created", "memory.updated", "memory.deleted",
                "consultation.completed"} <= VALID_WEBHOOK_EVENTS

    def test_router_registered_in_app(self):
        import mnemos.api.main as api_server
        paths = {r.path for r in api_server.app.routes}
        webhook_paths = [p for p in paths if p.startswith("/v1/webhooks")]
        assert len(webhook_paths) >= 3, f"expected webhook routes, got: {webhook_paths}"


# ── Signature correctness ────────────────────────────────────────────────────


class TestWebhookSignature:
    """HMAC-SHA256 signature over raw body bytes."""

    def test_sign_matches_receiver_verification(self):
        from mnemos.webhooks.dispatcher import _sign

        secret = "test-secret-abc123"
        body = '{"event":"memory.created","data":{"id":"mem_x"}}'

        expected = hmac.new(
            secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        assert _sign(secret, body) == expected

    def test_sign_different_secrets_produce_different_signatures(self):
        from mnemos.webhooks.dispatcher import _sign

        body = '{"x": 1}'
        assert _sign("secret-a", body) != _sign("secret-b", body)

    def test_sign_different_bodies_produce_different_signatures(self):
        from mnemos.webhooks.dispatcher import _sign

        secret = "same"
        assert _sign(secret, '{"a":1}') != _sign(secret, '{"a":2}')

    def test_sign_is_hex_string_of_expected_length(self):
        from mnemos.webhooks.dispatcher import _sign

        sig = _sign("k", "body")
        assert len(sig) == 64  # SHA-256 hex is 64 chars
        int(sig, 16)  # must parse as hex


# ── Event validation ─────────────────────────────────────────────────────────


class TestEventValidation:
    """The handler's event allowlist."""

    def test_valid_events_accepted(self):
        from mnemos.api.routes.webhooks import _validate_events

        # None of these should raise
        _validate_events(["memory.created"])
        _validate_events(["memory.created", "memory.updated", "memory.deleted"])
        _validate_events(["consultation.completed"])

    def test_empty_events_rejected(self):
        from fastapi import HTTPException

        from mnemos.api.routes.webhooks import _validate_events

        with pytest.raises(HTTPException) as exc:
            _validate_events([])
        assert exc.value.status_code == 422

    def test_unknown_event_rejected(self):
        from fastapi import HTTPException

        from mnemos.api.routes.webhooks import _validate_events

        with pytest.raises(HTTPException) as exc:
            _validate_events(["memory.created", "totally.made.up"])
        assert exc.value.status_code == 422
        assert "totally.made.up" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_url_validation(self, monkeypatch):
        """URL validator rejects bad schemes, SSRF targets, and metadata hosts."""
        from fastapi import HTTPException

        from mnemos.api.routes import webhooks as wh
        from mnemos.webhooks import validation as webhook_validation

        async def _fake_resolve_addrs(host: str):
            if host == "localhost":
                return ["127.0.0.1"]
            return ["93.184.216.34"]

        monkeypatch.setattr(webhook_validation, "_resolve_addrs", _fake_resolve_addrs)

        # Public host — fake DNS keeps the hostname-resolution path covered
        # without requiring external DNS in CI/sandboxed runners.
        await wh._validate_url("https://example.com/hook")

        for bad in (
            "ftp://example.com/hook",
            "file:///etc/passwd",
            "example.com",
        ):
            with pytest.raises(HTTPException):
                await wh._validate_url(bad)

        # SSRF guards: loopback + link-local metadata IPs must be rejected.
        for blocked in (
            "http://localhost/hook",
            "http://127.0.0.1/hook",
            "http://169.254.169.254/latest/meta-data",
            "http://metadata.google.internal/",
        ):
            with pytest.raises(HTTPException):
                await wh._validate_url(blocked)


# ── Retry schedule constants ─────────────────────────────────────────────────


class TestRetrySchedule:
    """Retry schedule matches documented contract (1m / 5m / 30m / 2h)."""

    def test_backoff_values(self):
        from mnemos.webhooks.dispatcher import BACKOFF_SCHEDULE
        assert BACKOFF_SCHEDULE == [60, 300, 1800, 7200]

    def test_max_attempts_matches_schedule_length(self):
        from mnemos.webhooks.dispatcher import BACKOFF_SCHEDULE, MAX_ATTEMPTS
        assert MAX_ATTEMPTS == len(BACKOFF_SCHEDULE)

    def test_delivery_timeout_reasonable(self):
        from mnemos.webhooks.dispatcher import DELIVERY_TIMEOUT
        assert 1.0 <= DELIVERY_TIMEOUT <= 60.0


# ── Integration: live DB required ────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(
    "MNEMOS_TEST_DB" not in os.environ,
    reason="set MNEMOS_TEST_DB=postgres://... to run integration tests",
)
class TestWebhookIntegration:
    """End-to-end tests requiring a live DB. Enable via MNEMOS_TEST_DB env."""

    @pytest.mark.asyncio
    async def test_webhook_crud_roundtrip(self):
        import asyncpg

        from mnemos.api.routes.webhooks import _to_item

        conn = await asyncpg.connect(os.environ["MNEMOS_TEST_DB"])
        try:
            # Insert
            row = await conn.fetchrow(
                """
                INSERT INTO webhook_subscriptions
                  (url, events, secret, owner_id, namespace)
                VALUES ($1, $2, $3, 'default', 'default')
                RETURNING id, url, events, description, owner_id, namespace,
                          created, revoked, revoked_at
                """,
                "https://test.example.com/hook",
                ["memory.created"],
                "test-secret",
            )
            item = _to_item(row)
            assert item.url == "https://test.example.com/hook"
            assert item.events == ["memory.created"]
            assert not item.revoked

            # Revoke
            await conn.execute(
                "UPDATE webhook_subscriptions SET revoked=TRUE, revoked_at=NOW() WHERE id=$1",
                row["id"],
            )
            # Cleanup
            await conn.execute(
                "DELETE FROM webhook_subscriptions WHERE id=$1", row["id"]
            )
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_dispatch_writes_delivery_rows(self):
        import asyncpg

        from mnemos.webhooks.dispatcher import dispatch

        conn = await asyncpg.connect(os.environ["MNEMOS_TEST_DB"])
        try:
            sub = await conn.fetchrow(
                """
                INSERT INTO webhook_subscriptions
                  (url, events, secret, owner_id, namespace)
                VALUES ('https://nonexistent.invalid/hook',
                        ARRAY['memory.created']::TEXT[],
                        'sec', 'default', 'default')
                RETURNING id
                """
            )

            try:
                await dispatch(
                    "memory.created",
                    {"memory_id": "mem_test"},
                    conn=conn,
                    owner_id="default", namespace="default",
                )

                rows = await conn.fetch(
                    "SELECT id, status, event_type, attempt_num "
                    "FROM webhook_deliveries WHERE subscription_id=$1",
                    sub["id"],
                )
                assert len(rows) >= 1
                assert rows[0]["event_type"] == "memory.created"
                assert rows[0]["attempt_num"] == 1
                # status starts pending; may become 'retrying' if the background
                # attempt fires before we observe — accept either.
                assert rows[0]["status"] in ("pending", "retrying", "abandoned")
            finally:
                await conn.execute(
                    "DELETE FROM webhook_deliveries WHERE subscription_id=$1",
                    sub["id"],
                )
                await conn.execute(
                    "DELETE FROM webhook_subscriptions WHERE id=$1", sub["id"]
                )
        finally:
            await conn.close()
