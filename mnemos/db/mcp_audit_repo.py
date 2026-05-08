"""MCP tool-call audit log repository.

Phase-D durable surface for the per-call audit trail. The Python
logger entries from `mnemos.mcp.tools._security._mcp_log_tool_audit`
remain (text-only, ephemeral), and this repo writes the same record
to the `mcp_audit_log` table when a Postgres pool is available.

The `parameter_shape` is already redacted at the call site by
`_mcp_parameter_shape` — only key names + value-type shape, never
raw values. So the table is safe to retain indefinitely under
normal data-protection policies.

Postgres-only by design. SQLite installs keep the logger-only
behavior; the schema lives in db/migrations_sqlite for operators
who run a custom query path, but the writer here is pg-only
(mirrors `mnemos.db.deletion_log` pattern).
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

VALID_OUTCOMES = {
    "called",
    "success",
    "failure",
    "error",
    "denied",
    "root_bypass",
}


def _looks_like_sqlite_conn(conn: Any) -> bool:
    module = type(conn).__module__.lower()
    name = type(conn).__name__.lower()
    return "sqlite" in module or "sqlite" in name


async def insert_audit_record(
    conn: Any,
    *,
    caller_user_id: str,
    role: str,
    tool: str,
    parameter_shape: dict[str, Any],
    outcome: str,
    error_class: str | None = None,
) -> bool:
    """Insert one audit row. Returns True on a real DB write, False if
    the connection is a SQLite handle (skipped, mirrors the
    deletion_log pattern)."""
    execute = getattr(conn, "execute", None)
    if conn is None or not callable(execute) or _looks_like_sqlite_conn(conn):
        return False

    if outcome not in VALID_OUTCOMES:
        # Defensive: callers should never pass arbitrary outcome
        # strings — keep the table in sync with the CHECK constraint
        # so an unexpected value doesn't surface as a generic
        # ConstraintError later.
        raise ValueError(
            f"invalid mcp_audit_log outcome {outcome!r}; "
            f"expected one of: {sorted(VALID_OUTCOMES)}"
        )

    await execute(
        """
        INSERT INTO mcp_audit_log (
            caller_user_id, role, tool, parameter_shape, outcome, error_class
        )
        VALUES ($1, $2, $3, $4::jsonb, $5, $6)
        """,
        caller_user_id,
        role,
        tool,
        json.dumps(parameter_shape, default=str, separators=(",", ":")),
        outcome,
        error_class,
    )
    return True


async def persist_audit_record_via_pool(
    *,
    caller_user_id: str,
    role: str,
    tool: str,
    parameter_shape: dict[str, Any],
    outcome: str,
    error_class: str | None = None,
) -> bool:
    """Acquire a connection from the lifecycle pool and write one row.

    Fire-and-forget by contract: failures are swallowed and logged at
    debug level. Returns True on successful write, False otherwise.

    Used by the MCP dispatcher's audit hook (`_mcp_log_tool_audit`)
    to persist the record without blocking tool dispatch on DB
    availability.

    Codex round-1 of #146: this path only fires inside the API
    process (which initializes the lifecycle pool). Standalone MCP
    bridges (mcp-stdio, mcp-http) don't have a pool — they fall
    back to `persist_audit_record_via_http` instead.
    """
    try:
        from mnemos.core import lifecycle as _lc

        pool_mgr = _lc.get_pool_manager()
    except Exception as exc:
        logger.debug("mcp_audit_log persist skipped (no pool): %s", exc)
        return False

    try:
        async with pool_mgr.acquire() as conn:
            return await insert_audit_record(
                conn,
                caller_user_id=caller_user_id,
                role=role,
                tool=tool,
                parameter_shape=parameter_shape,
                outcome=outcome,
                error_class=error_class,
            )
    except Exception as exc:
        logger.debug(
            "mcp_audit_log persist failed (caller=%s tool=%s): %s",
            caller_user_id,
            tool,
            exc,
        )
        return False


async def persist_audit_record_via_http(
    *,
    tool: str,
    parameter_shape: dict[str, Any],
    outcome: str,
    error_class: str | None = None,
) -> bool:
    """POST one audit record to the API's `/v1/internal/mcp_audit`.

    Used by standalone MCP bridges (mcp-stdio, mcp-http) that don't
    own the lifecycle pool. The API endpoint derives caller_user_id
    + role from the auth context, so this body only carries the
    MCP-specific fields. The bridge's own bearer token authenticates
    the request — same token used for the underlying tool's REST
    calls.

    Round-2 of #146: prefer the active MCP backend context's api_key
    over settings.server.api_key. In per-user MCP mode
    (MNEMOS_MCP_TOKENS=user:mcp_token:api_key), each call carries
    its own backend api_key via context. Without this preference,
    the audit row would either fail (no global key) or be
    misattributed to the global-key user.

    Fire-and-forget: failures are swallowed at debug level. Returns
    True on HTTP 204, False otherwise.
    """
    try:
        from mnemos.core.config import get_settings
    except Exception as exc:
        logger.debug("mcp_audit_log http skipped (no settings): %s", exc)
        return False

    # Prefer the per-call MCP backend context api_key (per-user mode)
    # over the global settings.server.api_key.
    api_key: str = ""
    try:
        from mnemos.mcp.tools._runtime import current_mcp_backend_api_key

        ctx_key = current_mcp_backend_api_key()
        if ctx_key:
            api_key = ctx_key
    except Exception:
        # Defensive: if the runtime helper isn't importable, fall
        # back to settings (single-user / API-process case).
        pass

    try:
        settings = get_settings()
        base = (settings.server.base or "").rstrip("/")
        if not api_key:
            api_key = settings.server.api_key or ""
    except Exception as exc:
        logger.debug("mcp_audit_log http skipped (settings error): %s", exc)
        return False

    if not base or not api_key:
        logger.debug(
            "mcp_audit_log http skipped (base=%r api_key_set=%s)",
            base, bool(api_key),
        )
        return False

    try:
        import httpx
    except ImportError:
        logger.debug("mcp_audit_log http skipped (httpx unavailable)")
        return False

    url = f"{base}/v1/internal/mcp_audit"
    payload: dict[str, Any] = {
        "tool": tool,
        "parameter_shape": parameter_shape,
        "outcome": outcome,
    }
    if error_class is not None:
        payload["error_class"] = error_class

    # Round-3 residual #1 of #146: include
    # `X-Mnemos-Audit-Token` when the service-only credential is
    # configured. Bridges must be in the same trust zone as the API
    # process to know this token (typically: identical
    # MNEMOS_INTERNAL_AUDIT_TOKEN env across both processes via the
    # same systemd EnvironmentFile / docker-compose env). When
    # unset, the endpoint operates in legacy bearer-token mode.
    headers: dict[str, str] = {"Authorization": f"Bearer {api_key}"}
    audit_token = (settings.server.internal_audit_token or "").strip()
    if audit_token:
        headers["X-Mnemos-Audit-Token"] = audit_token

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.post(
                url,
                json=payload,
                headers=headers,
            )
        if r.status_code in (200, 204):
            return True
        logger.debug(
            "mcp_audit_log http write returned %s for tool=%s",
            r.status_code, tool,
        )
        return False
    except Exception as exc:
        logger.debug(
            "mcp_audit_log http write failed (tool=%s): %s",
            tool, exc,
        )
        return False


async def persist_audit_record(
    *,
    caller_user_id: str,
    role: str,
    tool: str,
    parameter_shape: dict[str, Any],
    outcome: str,
    error_class: str | None = None,
) -> bool:
    """Try the in-process pool first; fall back to HTTP if no pool.

    Single entry point used by the dispatcher hook
    `_mcp_log_tool_audit`. The API process (with lifecycle pool) takes
    the fast path; standalone MCP bridges hit the HTTP fallback.
    """
    written = await persist_audit_record_via_pool(
        caller_user_id=caller_user_id,
        role=role,
        tool=tool,
        parameter_shape=parameter_shape,
        outcome=outcome,
        error_class=error_class,
    )
    if written:
        return True

    # No pool (standalone MCP bridge process) — fall back to HTTP.
    # caller_user_id + role are NOT sent in the body; the API
    # endpoint derives them from auth so a bridge can't forge a
    # different attribution.
    return await persist_audit_record_via_http(
        tool=tool,
        parameter_shape=parameter_shape,
        outcome=outcome,
        error_class=error_class,
    )
