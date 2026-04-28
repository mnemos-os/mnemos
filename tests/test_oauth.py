"""OAuth subsystem tests — wiring, provisioning logic, session lifecycle.

Unit tests run without DB or authlib (they exercise pure-Python paths).
Integration tests require MNEMOS_TEST_DB.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Module wiring smoke tests ─────────────────────────────────────────────────


class TestOAuthWiring:
    def test_oauth_module_imports(self):
        from mnemos.core import oauth
        # Public surface
        for name in (
            "SESSION_COOKIE_NAME",
            "SESSION_TTL",
            "start_login",
            "finish_login",
            "provision_or_link_user",
            "create_session",
            "resolve_session",
            "revoke_session",
            "revoke_all_sessions",
            "gc_expired_sessions",
        ):
            assert hasattr(oauth, name), f"mnemos.core.oauth missing: {name}"

    def test_oauth_handler_router(self):
        from mnemos.api.routes import oauth as handler
        assert hasattr(handler, "router")
        assert handler.router.prefix == "/auth/oauth"

    def test_oauth_models(self):
        from mnemos.domain.models import (
            OAuthProviderCreateRequest,
        )
        # Must instantiate with minimal args
        req = OAuthProviderCreateRequest(
            name="test",
            display_name="Test",
            kind="oidc",
            issuer_url="https://example.com",
            client_id="cid",
            client_secret="csecret",
        )
        assert req.scope == "openid profile email"
        assert req.enabled is True

    def test_router_registered_in_app(self):
        import mnemos.api.main as api_server
        paths = {r.path for r in api_server.app.routes}
        auth_paths = [p for p in paths if p.startswith("/auth/oauth")]
        assert len(auth_paths) >= 3, f"expected oauth routes, got: {auth_paths}"
        admin_oauth_paths = [p for p in paths if p.startswith("/admin/oauth")]
        assert len(admin_oauth_paths) >= 3, f"expected admin oauth routes, got: {admin_oauth_paths}"


# ── Pure helpers ─────────────────────────────────────────────────────────────


class TestOAuthHelpers:
    def test_mint_user_id_sanitizes(self):
        from mnemos.core.oauth import _mint_user_id
        uid = _mint_user_id("google", "12345|user@ex.com/nested")
        # Result should be safe chars only, length-capped
        assert len(uid) <= 64
        assert "@" not in uid
        assert "/" not in uid
        assert "|" not in uid
        assert uid.startswith("google:")

    def test_extract_external_id_prefers_sub(self):
        from mnemos.core.oauth import _extract_external_id
        assert _extract_external_id("google", {"sub": "abc", "email": "x@y"}) == "abc"

    def test_extract_external_id_github_id_fallback(self):
        from mnemos.core.oauth import _extract_external_id
        # GitHub userinfo returns 'id', not 'sub'
        assert _extract_external_id("github", {"id": 12345, "email": "x@y"}) == "12345"

    def test_extract_external_id_azure_oid_fallback(self):
        from mnemos.core.oauth import _extract_external_id
        assert _extract_external_id("azure-ad", {"oid": "abc-def", "sub": "xyz"}) == "xyz"
        # When sub absent, falls to oid
        assert _extract_external_id("azure-ad", {"oid": "abc-def"}) == "abc-def"

    def test_extract_external_id_email_last_resort(self):
        from mnemos.core.oauth import _extract_external_id
        assert _extract_external_id("custom", {"email": "x@y.com"}) == "email:x@y.com"

    def test_extract_external_id_none(self):
        from mnemos.core.oauth import _extract_external_id
        assert _extract_external_id("custom", {}) is None

    def test_discovery_url_handles_trailing_slash(self):
        from mnemos.core.oauth import _discovery_url
        assert _discovery_url("https://accounts.google.com") == \
            "https://accounts.google.com/.well-known/openid-configuration"
        assert _discovery_url("https://accounts.google.com/") == \
            "https://accounts.google.com/.well-known/openid-configuration"


# ── Session token entropy ────────────────────────────────────────────────────


class TestSessionTokens:
    def test_session_ttl_reasonable(self):
        from mnemos.core.oauth import SESSION_COOKIE_MAX_AGE, SESSION_TTL
        assert SESSION_TTL.total_seconds() >= 86400      # at least one day
        assert SESSION_COOKIE_MAX_AGE == int(SESSION_TTL.total_seconds())


# ── Integration ──────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(
    "MNEMOS_TEST_DB" not in os.environ,
    reason="set MNEMOS_TEST_DB=postgres://... to run integration tests",
)
class TestOAuthIntegration:
    @pytest.mark.asyncio
    async def test_session_create_and_resolve(self):
        from unittest.mock import MagicMock

        import asyncpg

        from mnemos.core.oauth import create_session, resolve_session, revoke_session

        conn = await asyncpg.connect(os.environ["MNEMOS_TEST_DB"])
        try:
            # Fake request object with minimal attributes
            fake_req = MagicMock()
            fake_req.headers = {"user-agent": "pytest"}
            fake_req.client = MagicMock()
            fake_req.client.host = "127.0.0.1"

            sid = await create_session(conn, "default", None, fake_req)
            assert len(sid) >= 40

            resolved = await resolve_session(conn, sid)
            assert resolved is not None
            assert resolved[0] == "default"

            assert await revoke_session(conn, sid) is True
            assert await resolve_session(conn, sid) is None

            # Cleanup
            await conn.execute(
                "DELETE FROM oauth_sessions WHERE session_id=$1", sid
            )
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_provision_new_user(self):
        import uuid

        import asyncpg

        from mnemos.core.oauth import provision_or_link_user

        conn = await asyncpg.connect(os.environ["MNEMOS_TEST_DB"])
        try:
            # Need a provider row; create a throwaway one
            provider_name = f"test-{uuid.uuid4().hex[:8]}"
            await conn.execute(
                """
                INSERT INTO oauth_providers
                  (name, display_name, kind, issuer_url, client_id, client_secret)
                VALUES ($1, 'Test', 'oidc', 'https://example.test', 'cid', 'csec')
                """,
                provider_name,
            )
            external_id = f"sub-{uuid.uuid4().hex[:8]}"
            claims = {
                "sub": external_id,
                "email": f"{uuid.uuid4().hex[:8]}@test.example",
                "name": "Test User",
            }
            user_id, identity_id = await provision_or_link_user(
                conn, provider_name, external_id, claims,
            )
            assert user_id.startswith(f"{provider_name}:") or ":" in user_id
            assert identity_id

            # Second call with same external_id returns same user/identity
            user_id_2, identity_id_2 = await provision_or_link_user(
                conn, provider_name, external_id, claims,
            )
            assert user_id == user_id_2
            assert identity_id == identity_id_2

            # Cleanup
            await conn.execute(
                "DELETE FROM oauth_identities WHERE provider=$1", provider_name,
            )
            await conn.execute("DELETE FROM users WHERE id=$1", user_id)
            await conn.execute(
                "DELETE FROM oauth_providers WHERE name=$1", provider_name,
            )
        finally:
            await conn.close()
