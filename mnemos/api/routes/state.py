"""State API routes for /state/{key} and /state

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

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.persistence_helpers import backend_or_503
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
    backend = backend_or_503()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        async with backend.transactional() as tx:
            rows = await backend.state_kv.list_namespace(
                tx,
                owner_id=target_owner,
                namespace=target_ns,
            )
        return {"keys": [dict(r) for r in rows]}
    except NotImplementedError as e:
        raise HTTPException(status_code=503, detail=str(e))
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
    backend = backend_or_503()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        async with backend.transactional() as tx:
            row = await backend.state_kv.get(
                tx,
                key,
                owner_id=target_owner,
                namespace=target_ns,
            )
        if not row:
            raise HTTPException(status_code=404, detail=f"State key '{key}' not found")
        result = dict(row)
        # StateManager.set persists the value as
        # ``MNEMOS_SM:v1:<json>`` so the in-process get/set round-
        # trip can disambiguate StateManager-written rows from
        # legacy opaque TEXT or REST-written objects. That marker
        # is INTERNAL — REST clients expect ``value`` to be the
        # JSON payload they posted, not the storage envelope.
        # Strip the prefix on read; pre-envelope rows pass through.
        from mnemos.domain.memory_categorization.state import _SM_ENVELOPE_PREFIX
        raw_value = result.get("value")
        if isinstance(raw_value, str) and raw_value.startswith(_SM_ENVELOPE_PREFIX):
            result["value"] = raw_value[len(_SM_ENVELOPE_PREFIX):]
        return result
    except HTTPException:
        raise
    except NotImplementedError as e:
        raise HTTPException(status_code=503, detail=str(e))
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
    backend = backend_or_503()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        async with backend.transactional() as tx:
            row = await backend.state_kv.set(
                tx,
                key,
                json.dumps(req.value),
                owner_id=target_owner,
                namespace=target_ns,
            )
        if row is None:
            raise HTTPException(
                status_code=409,
                detail="State key is reserved by a soft-deleted row; restore before updating it",
            )
        return dict(row)
    except HTTPException:
        raise
    except NotImplementedError as e:
        raise HTTPException(status_code=503, detail=str(e))
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
    backend = backend_or_503()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        async with backend.transactional() as tx:
            deleted = await backend.state_kv.delete(
                tx,
                key,
                owner_id=target_owner,
                namespace=target_ns,
            )
    except NotImplementedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail=f"State key '{key}' not found")
