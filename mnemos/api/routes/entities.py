"""Entities API: CRUD for tracked entities (people, projects, concepts).

Per-owner, per-namespace entity registry. Each
`(owner_id, namespace, entity_type, name)` is unique, and entities from one
namespace are invisible to another. Root may cross-read by passing
`?owner_id=<target>&namespace=<target>`.
"""
import json
import logging
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes._postgres_only import _require_postgres_backend
from mnemos.core.security import assert_owned_context, is_root, scope_namespace, scope_owner

logger = logging.getLogger(__name__)
router = APIRouter(tags=["entities"])


def _require_entities_backend() -> None:
    _require_postgres_backend()


class EntityCreateRequest(BaseModel):
    # #171: Literal[...] enforces the same set ENTITY_TYPES contains.
    # The values are duplicated here (not Literal[*ENTITY_TYPES] because
    # PEP 646 unpacking doesn't work in Literal contexts in stable
    # Python yet); a regression test in tests/test_entities_route.py
    # asserts the two stay in sync.
    entity_type: Literal[
        "person", "project", "concept", "document", "decision", "event"
    ]
    name: str
    description: Optional[str] = None
    metadata: Optional[dict] = None


class EntityUpdateRequest(BaseModel):
    description: Optional[str] = None
    metadata: Optional[dict] = None


class EntityLinkRequest(BaseModel):
    related_id: str

@router.post("/entities", status_code=201)
async def create_entity(
    req: EntityCreateRequest,
    user: UserContext = Depends(get_current_user),
):
    # #171: entity_type is now Literal[...] in the request model —
    # Pydantic auto-422s on invalid values before we get here.
    _require_entities_backend()
    try:
        entity_id = str(uuid.uuid4())
        async with _lc.get_pool_manager().transactional() as conn:
            row = await conn.fetchrow(
                '''INSERT INTO entities (id, owner_id, namespace, entity_type, name, description, metadata)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                   ON CONFLICT (owner_id, namespace, entity_type, name) DO UPDATE
                   SET description = COALESCE($6, entities.description),
                       updated = NOW()
                   WHERE entities.deleted_at IS NULL
                   RETURNING id::text, entity_type, name, description, metadata, created::text, updated::text''',
                entity_id, user.user_id, user.namespace,
                req.entity_type, req.name,
                req.description, json.dumps(req.metadata or {})
            )
        if row is None:
            raise HTTPException(
                status_code=409,
                detail="Entity name is reserved by a soft-deleted row; restore before updating it",
            )
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating entity: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/entities")
async def list_entities(
    entity_type: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None),
    namespace: Optional[str] = Query(None),
):
    """List entities. Non-root callers see only their own
    (owner_id, namespace) slice. Root may pass ?owner_id= and/or
    ?namespace= to target another tenant for audit/support.
    """
    _require_entities_backend()
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        async with _lc.get_pool_manager().acquire() as conn:
            if entity_type and search:
                rows = await conn.fetch(
                    '''SELECT id::text, entity_type, name, description, metadata, created::text, updated::text
                       FROM entities WHERE owner_id=$1 AND namespace=$2 AND entity_type=$3 AND name ILIKE $4
                         AND deleted_at IS NULL
                       ORDER BY name LIMIT $5''',
                    target_owner, target_ns, entity_type, f'%{search}%', limit
                )
            elif entity_type:
                rows = await conn.fetch(
                    '''SELECT id::text, entity_type, name, description, metadata, created::text, updated::text
                       FROM entities WHERE owner_id=$1 AND namespace=$2 AND entity_type=$3
                         AND deleted_at IS NULL
                       ORDER BY name LIMIT $4''',
                    target_owner, target_ns, entity_type, limit
                )
            elif search:
                rows = await conn.fetch(
                    '''SELECT id::text, entity_type, name, description, metadata, created::text, updated::text
                       FROM entities WHERE owner_id=$1 AND namespace=$2 AND (name ILIKE $3 OR description ILIKE $3)
                         AND deleted_at IS NULL
                       ORDER BY name LIMIT $4''',
                    target_owner, target_ns, f'%{search}%', limit
                )
            else:
                rows = await conn.fetch(
                    '''SELECT id::text, entity_type, name, description, metadata, created::text, updated::text
                       FROM entities WHERE owner_id=$1 AND namespace=$2
                         AND deleted_at IS NULL
                       ORDER BY entity_type, name LIMIT $3''',
                    target_owner, target_ns, limit
                )
        return {"entities": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        logger.error(f"Error listing entities: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/entities/{entity_id}")
async def get_entity(entity_id: str, user: UserContext = Depends(get_current_user)):
    _require_entities_backend()
    async with _lc.get_pool_manager().acquire() as conn:
        owner, namespace = await assert_owned_context(conn, "entities", entity_id, user)
        row = await conn.fetchrow(
            '''SELECT id::text, entity_type, name, description, metadata,
                      related_entities, created::text, updated::text
               FROM entities
               WHERE id = $1::uuid AND owner_id = $2 AND namespace = $3
                 AND deleted_at IS NULL''',
            entity_id, owner, namespace,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Entity not found")
    return dict(row)


@router.patch("/entities/{entity_id}")
async def update_entity(
    entity_id: str,
    req: EntityUpdateRequest,
    user: UserContext = Depends(get_current_user),
):
    _require_entities_backend()
    updates = {}
    if req.description is not None:
        updates['description'] = req.description
    if req.metadata is not None:
        updates['metadata'] = req.metadata
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    try:
        async with _lc.get_pool_manager().transactional() as conn:
            # Re-assert owner + namespace in the UPDATE so concurrent tenancy
            # changes between the probe and write cannot land on the wrong row.
            owner, namespace = await assert_owned_context(conn, "entities", entity_id, user)
            if 'description' in updates and 'metadata' in updates:
                row = await conn.fetchrow(
                    '''UPDATE entities SET description=$1, metadata=$2::jsonb, updated=NOW()
                       WHERE id=$3::uuid AND owner_id=$4 AND namespace=$5
                         AND deleted_at IS NULL
                       RETURNING id::text, entity_type, name, description, metadata, created::text, updated::text''',
                    updates['description'], json.dumps(updates['metadata']), entity_id, owner, namespace,
                )
            elif 'description' in updates:
                row = await conn.fetchrow(
                    '''UPDATE entities SET description=$1, updated=NOW()
                       WHERE id=$2::uuid AND owner_id=$3 AND namespace=$4
                         AND deleted_at IS NULL
                       RETURNING id::text, entity_type, name, description, metadata, created::text, updated::text''',
                    updates['description'], entity_id, owner, namespace,
                )
            else:
                row = await conn.fetchrow(
                    '''UPDATE entities SET metadata=$1::jsonb, updated=NOW()
                       WHERE id=$2::uuid AND owner_id=$3 AND namespace=$4
                         AND deleted_at IS NULL
                       RETURNING id::text, entity_type, name, description, metadata, created::text, updated::text''',
                    json.dumps(updates['metadata']), entity_id, owner, namespace,
                )
        if not row:
            raise HTTPException(status_code=404, detail="Entity not found")
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating entity: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/entities/{entity_id}/link", status_code=200)
async def link_entities(
    entity_id: str,
    req: EntityLinkRequest,
    user: UserContext = Depends(get_current_user),
):
    """Link two entities bidirectionally via related_entities UUID[] array.

    Both entities must be owned by the caller (or caller must be root).
    """
    _require_entities_backend()
    try:
        async with _lc.get_pool_manager().transactional() as conn:
            owner, namespace = await assert_owned_context(conn, "entities", entity_id, user)
            related_owner, related_namespace = await assert_owned_context(conn, "entities", req.related_id, user)
            # Link A->B
            await conn.execute(
                '''UPDATE entities
                   SET related_entities = array_append(
                       COALESCE(related_entities, ARRAY[]::uuid[]), $2::uuid
                   ), updated = NOW()
                   WHERE id = $1::uuid
                   AND owner_id = $3
                   AND namespace = $4
                   AND deleted_at IS NULL
                   AND NOT ($2::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[])))''',
                entity_id, req.related_id, owner, namespace,
            )
            # Link B->A
            await conn.execute(
                '''UPDATE entities
                   SET related_entities = array_append(
                       COALESCE(related_entities, ARRAY[]::uuid[]), $2::uuid
                   ), updated = NOW()
                   WHERE id = $1::uuid
                   AND owner_id = $3
                   AND namespace = $4
                   AND deleted_at IS NULL
                   AND NOT ($2::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[])))''',
                req.related_id, entity_id, related_owner, related_namespace,
            )
        return {"status": "linked", "entity_id": entity_id, "related_id": req.related_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error linking entities {entity_id} <-> {req.related_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/entities/{entity_id}", status_code=204)
async def delete_entity(entity_id: str, user: UserContext = Depends(get_current_user)):
    _require_entities_backend()
    try:
        async with _lc.get_pool_manager().transactional() as conn:
            owner, namespace = await assert_owned_context(conn, "entities", entity_id, user)
            # Remove from other entities' arrays (caller's own only; root clears all)
            if is_root(user):
                await conn.execute(
                    '''UPDATE entities
                       SET related_entities = array_remove(related_entities, $1::uuid)
                       WHERE deleted_at IS NULL
                         AND $1::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[]))''',
                    entity_id
                )
            else:
                await conn.execute(
                    '''UPDATE entities
                       SET related_entities = array_remove(related_entities, $1::uuid)
                       WHERE owner_id = $2
                       AND namespace = $3
                       AND deleted_at IS NULL
                       AND $1::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[]))''',
                    entity_id, owner, namespace,
                )
            result = await conn.execute(
                'DELETE FROM entities WHERE id = $1::uuid AND owner_id = $2 AND namespace = $3 AND deleted_at IS NULL',
                entity_id, owner, namespace,
            )
        if result == 'DELETE 0':
            raise HTTPException(status_code=404, detail="Entity not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error deleting entity {entity_id}: {e}", exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/entities/{entity_id}/related")
async def get_related_entities(entity_id: str, user: UserContext = Depends(get_current_user)):
    _require_entities_backend()
    async with _lc.get_pool_manager().acquire() as conn:
        target_owner, target_ns = await assert_owned_context(conn, "entities", entity_id, user)
        entity = await conn.fetchrow(
            '''SELECT related_entities FROM entities
               WHERE id = $1::uuid AND owner_id = $2 AND namespace = $3
                 AND deleted_at IS NULL''',
            entity_id, target_owner, target_ns,
        )
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    related_ids = entity['related_entities'] or []
    if not related_ids:
        return {"related": []}
    async with _lc.get_pool_manager().acquire() as conn:
        # Only surface related entities visible to the caller (same owner, or root).
        if is_root(user):
            rows = await conn.fetch(
                '''SELECT id::text, entity_type, name, description, metadata, created::text, updated::text
                   FROM entities WHERE id = ANY($1::uuid[])
                     AND deleted_at IS NULL''',
                related_ids
            )
        else:
            rows = await conn.fetch(
                '''SELECT id::text, entity_type, name, description, metadata, created::text, updated::text
                   FROM entities WHERE owner_id = $1 AND namespace = $2 AND id = ANY($3::uuid[])
                     AND deleted_at IS NULL''',
                target_owner, target_ns, related_ids
            )
    return {"related": [dict(r) for r in rows]}
