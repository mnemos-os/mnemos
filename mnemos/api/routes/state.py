"""State API: GET/PUT/DELETE /state/{key}, GET /state

Per-owner, per-namespace KV store. All operations are scoped to the caller's
`user.user_id` and `user.namespace`; keys from one namespace are invisible to
another. Root can target another owner/namespace via `?owner_id=` and
`?namespace=`.
"""
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes._postgres_only import _require_postgres_backend
from mnemos.core.security import scope_namespace, scope_owner

logger = logging.getLogger(__name__)
router = APIRouter(tags=["state"])


class StateSetRequest(BaseModel):
    value: Any

@router.get("/state")
async def list_state_keys(
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None, description="Admin-only: target another owner"),
    namespace: Optional[str] = Query(None, description="Admin-only: target another namespace"),
):
    _require_postgres_backend()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        async with _lc.get_pool_manager().acquire() as conn:
            rows = await conn.fetch(
                'SELECT key, updated::text, version FROM state '
                'WHERE owner_id = $1 AND namespace = $2 ORDER BY key',
                target_owner, target_ns,
            )
        return {"keys": [dict(r) for r in rows]}
    except Exception as e:
        logger.error(f"Error listing state keys: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/state/{key}")
async def get_state(
    key: str,
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None),
    namespace: Optional[str] = Query(None),
):
    _require_postgres_backend()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        async with _lc.get_pool_manager().acquire() as conn:
            row = await conn.fetchrow(
                'SELECT key, value, updated::text, version FROM state '
                'WHERE owner_id = $1 AND namespace = $2 AND key = $3',
                target_owner, target_ns, key,
            )
        if not row:
            raise HTTPException(status_code=404, detail=f"State key '{key}' not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting state key: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/state/{key}", status_code=200)
async def set_state(
    key: str,
    req: StateSetRequest,
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None, description="Admin-only: write on behalf of another owner"),
    namespace: Optional[str] = Query(None, description="Admin-only: write into another namespace"),
):
    _require_postgres_backend()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        async with _lc.get_pool_manager().transactional() as conn:
            row = await conn.fetchrow(
                '''INSERT INTO state (owner_id, namespace, key, value, updated)
                   VALUES ($1, $2, $3, $4::jsonb, NOW())
                   ON CONFLICT (owner_id, namespace, key) DO UPDATE
                   SET value = $4::jsonb, updated = NOW(), version = state.version + 1
                   RETURNING key, value, updated::text, version''',
                target_owner, target_ns, key, json.dumps(req.value),
            )
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting state key '{key}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/state/{key}", status_code=204)
async def delete_state(
    key: str,
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None),
    namespace: Optional[str] = Query(None),
):
    _require_postgres_backend()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    async with _lc.get_pool_manager().transactional() as conn:
        result = await conn.execute(
            'DELETE FROM state WHERE owner_id = $1 AND namespace = $2 AND key = $3',
            target_owner, target_ns, key,
        )
    if result == 'DELETE 0':
        raise HTTPException(status_code=404, detail=f"State key '{key}' not found")
