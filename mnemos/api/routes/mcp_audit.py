"""Internal MCP audit endpoint.

The MCP bridge processes (`mnemos serve mcp-stdio` and
`mnemos serve mcp-http`) run in their own Python processes, talk to
MNEMOS via REST, and don't initialize the lifecycle Postgres pool.
For Phase-D durable audit they POST records to this endpoint, which
runs INSIDE the API process and owns the pool.

The endpoint is internal (not part of the public REST surface) but
is mounted on the same FastAPI app and authenticated via the same
bearer-token mechanism as other routes. caller_user_id and role
are derived from the auth context — the body only carries the
MCP-specific fields (tool name, redacted parameter shape, outcome,
optional error class).

Codex round-1 of #146: prior to this endpoint the durable write
was wired only to the in-process lifecycle pool, so audit records
from standalone MCP bridges never landed in the table while the
limitation doc claimed Phase-D shipped.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import require_postgres_pool_or_503
from mnemos.core.config import get_settings
from mnemos.db.mcp_audit_repo import VALID_OUTCOMES, insert_audit_record

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/internal", tags=["internal"])

# Round-2 + round-3 of #146: strict shape validator for audit
# parameter_shape. The body is operator-supplied; without validation,
# any token holder could append rows with raw secrets in
# `parameter_shape` (defeating the redaction guarantee documented
# for this table). Lock the shape down to exactly what
# `_mcp_parameter_shape()` emits:
#   {<key>: {"type": <str>, "length"?: int, "count"?: int,
#            "item_types"?: [<str>, ...]}}
# Round-3: closed allowlist for `type` and `item_types` entries.
# Round-2 only filtered length/whitespace, allowing values like
# {"type": "sk_live_secret"} to slip through.
_ALLOWED_SHAPE_TYPE_NAMES = frozenset({
    "str", "bool", "int", "float", "list", "dict", "none",
    # Common Python primitive type names for unusual MCP inputs.
    "bytes", "tuple", "set", "frozenset", "NoneType",
})
# #158: cleaned up _MAX_PARAMETER_SHAPE_TYPE_NAME — it was defined
# but never read. The closed allowlist
# (`_ALLOWED_SHAPE_TYPE_NAMES`) is strictly more restrictive than any
# 32-char ceiling would be, so the constant was dead code.
_MAX_PARAMETER_SHAPE_KEYS = 64
_MAX_PARAMETER_SHAPE_KEY_LENGTH = 128
_MAX_PARAMETER_SHAPE_ITEM_TYPES = 16


def _validate_parameter_shape(value: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("parameter_shape must be an object")
    if len(value) > _MAX_PARAMETER_SHAPE_KEYS:
        raise ValueError(
            f"parameter_shape has too many keys "
            f"(max {_MAX_PARAMETER_SHAPE_KEYS})"
        )
    for key, entry in value.items():
        if not isinstance(key, str):
            raise ValueError("parameter_shape keys must be strings")
        if len(key) > _MAX_PARAMETER_SHAPE_KEY_LENGTH:
            raise ValueError(
                f"parameter_shape key {key[:32]!r} exceeds max length "
                f"({_MAX_PARAMETER_SHAPE_KEY_LENGTH})"
            )
        if not isinstance(entry, dict):
            raise ValueError(
                f"parameter_shape[{key}] must be an object"
            )
        # Allowed entry keys: type (required), length, count, item_types.
        allowed = {"type", "length", "count", "item_types"}
        extra = set(entry) - allowed
        if extra:
            raise ValueError(
                f"parameter_shape[{key}] has unexpected fields: "
                f"{sorted(extra)}"
            )
        type_name = entry.get("type")
        if not isinstance(type_name, str):
            raise ValueError(
                f"parameter_shape[{key}].type must be a string"
            )
        # Round-3: closed allowlist. Earlier round only checked length
        # and whitespace, so values like "sk_live_secret" slipped
        # through as raw secrets. Real MCP inputs only ever produce
        # JSON primitive type names + a few Python type names.
        if type_name not in _ALLOWED_SHAPE_TYPE_NAMES:
            raise ValueError(
                f"parameter_shape[{key}].type {type_name!r} is not in "
                f"the allowed type allowlist (raw values forbidden)"
            )
        if "length" in entry and not isinstance(entry["length"], int):
            raise ValueError(
                f"parameter_shape[{key}].length must be int"
            )
        if "count" in entry and not isinstance(entry["count"], int):
            raise ValueError(
                f"parameter_shape[{key}].count must be int"
            )
        if "item_types" in entry:
            item_types = entry["item_types"]
            if not isinstance(item_types, list):
                raise ValueError(
                    f"parameter_shape[{key}].item_types must be a list"
                )
            if len(item_types) > _MAX_PARAMETER_SHAPE_ITEM_TYPES:
                raise ValueError(
                    f"parameter_shape[{key}].item_types too long"
                )
            for item in item_types:
                if not isinstance(item, str):
                    raise ValueError(
                        f"parameter_shape[{key}].item_types entries must be strings"
                    )
                # Round-3: same closed allowlist applies to item_types.
                if item not in _ALLOWED_SHAPE_TYPE_NAMES:
                    raise ValueError(
                        f"parameter_shape[{key}].item_types entry {item!r} "
                        f"is not in the allowed type allowlist"
                    )
    return value


class MCPAuditRequest(BaseModel):
    tool: str = Field(..., min_length=1, max_length=128)
    parameter_shape: Dict[str, Any] = Field(default_factory=dict)
    outcome: str = Field(..., min_length=1)
    error_class: Optional[str] = Field(default=None, max_length=128)

    @field_validator("outcome")
    @classmethod
    def _valid_outcome(cls, v: str) -> str:
        if v not in VALID_OUTCOMES:
            raise ValueError(
                f"invalid outcome {v!r}; expected one of: "
                f"{sorted(VALID_OUTCOMES)}"
            )
        return v

    @field_validator("parameter_shape")
    @classmethod
    def _shape_is_redacted(cls, v: Dict[str, Any]) -> Dict[str, Any]:
        # Lock the shape down to what _mcp_parameter_shape() emits.
        # Any token holder can hit this endpoint, so we cannot trust
        # the body to carry only redacted entries — enforce here.
        return _validate_parameter_shape(v)


def _require_internal_audit_token(
    x_mnemos_audit_token: Optional[str] = Header(default=None),
) -> None:
    """Round-3 residual #1 of #146: lock /v1/internal/mcp_audit to a
    service-only credential.

    Reject the request unless the request carries the configured
    `MNEMOS_INTERNAL_AUDIT_TOKEN` in the `X-Mnemos-Audit-Token`
    header. Constant-time compare to avoid timing leaks. When the
    token is unset (legacy / not yet configured), the endpoint
    operates in legacy bearer-token mode — any authenticated user
    can still POST. The legacy mode is a transition state; the
    audit endpoint should be locked down before treating the table
    as a tamper-resistant audit source.
    """
    settings = get_settings()
    expected = (settings.server.internal_audit_token or "").strip()
    if not expected:
        # Legacy mode: token not configured. Fall through to
        # bearer-token auth on the route. Tracked as a residual in
        # KNOWN_LIMITATIONS.md.
        return None
    presented = (x_mnemos_audit_token or "").strip()
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=401,
            detail=(
                "missing or invalid X-Mnemos-Audit-Token header. "
                "This endpoint is locked to a service-only credential "
                "(MNEMOS_INTERNAL_AUDIT_TOKEN) when configured."
            ),
        )
    return None


@router.post("/mcp_audit", status_code=204)
async def write_mcp_audit_record(
    body: MCPAuditRequest = Body(...),
    user: UserContext = Depends(get_current_user),
    _: None = Depends(_require_internal_audit_token),
) -> None:
    """Persist one MCP tool-call audit record.

    Authenticated callers identify themselves via the bearer token
    they used to call the underlying tool. caller_user_id and role
    come from `user` (auth context) — clients cannot forge a
    different attribution by setting body fields.

    Round-3 residual #1: when MNEMOS_INTERNAL_AUDIT_TOKEN is set,
    the request must also carry X-Mnemos-Audit-Token matching that
    value. This locks the endpoint to bridges and prevents normal
    API token holders from forging audit rows. The bearer token
    still establishes caller_user_id/role attribution.
    """
    require_postgres_pool_or_503(route_label="POST /v1/internal/mcp_audit")
    async with _lc.get_pool_manager().acquire() as conn:
        try:
            await insert_audit_record(
                conn,
                caller_user_id=user.user_id or "unknown",
                role=user.role or "unknown",
                tool=body.tool,
                parameter_shape=body.parameter_shape,
                outcome=body.outcome,
                error_class=body.error_class,
            )
        except ValueError as exc:
            # Repo enforces VALID_OUTCOMES too; the field validator
            # should catch upstream, but defense-in-depth.
            raise HTTPException(status_code=400, detail=str(exc))
    return None
