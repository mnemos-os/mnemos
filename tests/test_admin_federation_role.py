"""Admin API must accept role='federation' — and the DB must allow it.

Regression for #M31-03. Before the fix:
  - mnemos/api/routes/admin.py validator rejected anything except ('user', 'root')
  - mnemos/api/routes/federation.py required role == 'federation'
  - db/migrations_v1_multiuser.sql declared CHECK (role IN ('user', 'root'))

All three sites had to be consistent; operators had to hand-write SQL
to onboard a federation peer. The v3.0 federation capability was
effectively root-or-SQL-only.

This test covers the static contract: the admin validator accepts
'federation', the federation handler's expected role is unchanged, and
the federation migration contains the CHECK-constraint relaxation that
the DB will execute on install/upgrade.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mnemos.api.routes.admin import create_user
from mnemos.domain.models import UserCreateRequest

REPO_ROOT = Path(__file__).parent.parent
FEDERATION_HANDLER = REPO_ROOT / "mnemos" / "api" / "routes" / "federation.py"
FEDERATION_MIGRATION = REPO_ROOT / "db" / "migrations_v3_federation.sql"


@pytest.fixture
def fake_db_pool(monkeypatch):
    """Mock the mnemos.core.lifecycle pool so the handler doesn't need a real DB."""
    from mnemos.core import lifecycle

    mock_conn = MagicMock()
    mock_conn.fetchrow = AsyncMock(side_effect=[
        None,  # existing check: no existing user
        {
            "id": "fed-peer-b",
            "display_name": "Peer B",
            "email": None,
            "role": "federation",
            "namespace": "default",
            "created_at": __import__("datetime").datetime(2026, 4, 22, 19, 30),
        },
    ])

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncContext(mock_conn))
    monkeypatch.setattr(lifecycle, "_pool", mock_pool)
    return mock_pool


class _AsyncContext:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
async def test_admin_accepts_federation_role(fake_db_pool):
    """POST /admin/users with role='federation' must succeed, not 422."""
    request = UserCreateRequest(
        id="fed-peer-b",
        display_name="Peer B",
        role="federation",
    )
    # create_user takes a UserContext for the root requirement but the
    # `require_root` dependency is bypassed when we call the function
    # directly — pass a minimal sentinel.
    result = await create_user(request, _=MagicMock(role="root"))
    assert result.role == "federation", (
        "Admin API rejected role='federation' — #M31-03 regression. "
        "Operators cannot onboard federation peers via the API."
    )


def test_admin_still_rejects_arbitrary_roles():
    """The allowlist must stay closed — arbitrary roles must still 422.

    #169: role is now Literal["user", "root", "federation"] on the
    Pydantic model, so invalid values are rejected at model parse
    time (which FastAPI surfaces as a 422). Test now exercises the
    Pydantic-level rejection directly. The intent — "allowlist
    stays closed" — is satisfied either way.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        UserCreateRequest(
            id="random-user",
            display_name="Random",
            role="superadmin",  # not in the allowlist
        )
    # The error message must reference the offending field.
    assert "role" in str(excinfo.value).lower()


def test_federation_handler_expects_federation_role():
    """Static check: federation.py's role guard still includes
    'federation'. If someone tightens it without updating admin.py,
    we would re-break the onboarding flow."""
    src = FEDERATION_HANDLER.read_text(encoding="utf-8")
    assert re.search(
        r"""role\s+not\s+in\s*\(\s*["']federation["'],\s*["']root["']\s*\)"""
        r"""|role\s+not\s+in\s*\(\s*["']root["'],\s*["']federation["']\s*\)""",
        src,
    ), (
        "mnemos/api/routes/federation.py no longer expects role='federation'. "
        "If the contract changed, admin.py must change to match."
    )


def test_migration_relaxes_users_role_check():
    """The DB CHECK constraint from v1_multiuser must be relaxed by
    v3_federation to include 'federation'. Without this, even the
    admin INSERT with the right role will fail at the DB level."""
    sql = FEDERATION_MIGRATION.read_text(encoding="utf-8")
    # Look for the idempotent drop-and-recreate pattern
    assert re.search(
        r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+users_role_check",
        sql,
        flags=re.IGNORECASE,
    ), (
        "v3_federation migration must drop the old CHECK constraint "
        "(idempotently) before re-adding the relaxed version."
    )
    assert re.search(
        r"CHECK\s*\(\s*role\s+IN\s*\(\s*['\"]user['\"]"
        r".*['\"]federation['\"]",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    ), (
        "v3_federation migration must add a CHECK constraint that "
        "allows role='federation'. Without it, admin INSERT hits the "
        "old CHECK from v1_multiuser and fails at DB level."
    )
