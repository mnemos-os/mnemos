"""Journal API: POST /journal, GET /journal, DELETE /journal/{entry_id}

Per-owner, per-namespace journal. Each entry is scoped to the creating user's
`user_id` and `namespace`; root can target another owner/namespace via
`?owner_id=` and `?namespace=`.
"""
import json
import logging
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user
from api.security import scope_namespace, scope_owner

logger = logging.getLogger(__name__)
router = APIRouter(tags=["journal"])


class JournalCreateRequest(BaseModel):
    topic: str
    content: str
    date: Optional[str] = None   # ISO date string; defaults to CURRENT_DATE if omitted
    metadata: Optional[dict] = None


class JournalEntry(BaseModel):
    id: str
    entry_date: str
    topic: Optional[str]
    content: Optional[str]
    metadata: Optional[dict]
    created: str

@router.post("/journal", status_code=201)
async def create_journal_entry(
    req: JournalCreateRequest,
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None, description="Admin-only: write on behalf of another owner"),
    namespace: Optional[str] = Query(None, description="Admin-only: write into another namespace"),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        entry_id = str(uuid.uuid4())
        async with _lc._pool.acquire() as conn:
            if req.date:
                try:
                    entry_date = date.fromisoformat(req.date)
                except ValueError:
                    raise HTTPException(status_code=422, detail="Invalid date format; expected YYYY-MM-DD")
                row = await conn.fetchrow(
                    '''INSERT INTO journal (id, owner_id, namespace, entry_date, topic, content, metadata)
                       VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                       RETURNING id, entry_date::text, topic, content, metadata, created::text''',
                    entry_id, target_owner, target_ns, entry_date, req.topic, req.content,
                    json.dumps(req.metadata or {}),
                )
            else:
                row = await conn.fetchrow(
                    '''INSERT INTO journal (id, owner_id, namespace, entry_date, topic, content, metadata)
                       VALUES ($1, $2, $3, CURRENT_DATE, $4, $5, $6::jsonb)
                       RETURNING id, entry_date::text, topic, content, metadata, created::text''',
                    entry_id, target_owner, target_ns, req.topic, req.content,
                    json.dumps(req.metadata or {}),
                )
        return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating journal entry: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/journal")
async def list_journal_entries(
    topic: Optional[str] = None,
    date_str: Optional[str] = Query(None, alias="date"),
    search: Optional[str] = None,
    limit: int = Query(20, ge=1, le=200),
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None),
    namespace: Optional[str] = Query(None),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    try:
        async with _lc._pool.acquire() as conn:
            if date_str:
                try:
                    parsed_date = date.fromisoformat(date_str)
                except ValueError:
                    raise HTTPException(status_code=422, detail="Invalid date format; expected YYYY-MM-DD")
                rows = await conn.fetch(
                    '''SELECT id, entry_date::text, topic, content, metadata, created::text
                       FROM journal WHERE owner_id = $1 AND namespace = $2 AND entry_date = $3
                       ORDER BY created DESC LIMIT $4''',
                    target_owner, target_ns, parsed_date, limit
                )
            elif topic:
                rows = await conn.fetch(
                    '''SELECT id, entry_date::text, topic, content, metadata, created::text
                       FROM journal WHERE owner_id = $1 AND namespace = $2 AND topic = $3
                       ORDER BY created DESC LIMIT $4''',
                    target_owner, target_ns, topic, limit
                )
            elif search:
                rows = await conn.fetch(
                    '''SELECT id, entry_date::text, topic, content, metadata, created::text
                       FROM journal WHERE owner_id = $1 AND namespace = $2 AND (content ILIKE $3 OR topic ILIKE $3)
                       ORDER BY created DESC LIMIT $4''',
                    target_owner, target_ns, f'%{search}%', limit
                )
            else:
                rows = await conn.fetch(
                    '''SELECT id, entry_date::text, topic, content, metadata, created::text
                       FROM journal WHERE owner_id = $1 AND namespace = $2
                       ORDER BY created DESC LIMIT $3''',
                    target_owner, target_ns, limit
                )
        return {"entries": [dict(r) for r in rows], "count": len(rows)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing journal entries: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/journal/{entry_id}", status_code=204)
async def delete_journal_entry(
    entry_id: str,
    user: UserContext = Depends(get_current_user),
    owner_id: Optional[str] = Query(None),
    namespace: Optional[str] = Query(None),
):
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    target_owner = scope_owner(user, owner_id)
    target_ns = scope_namespace(user, namespace)
    async with _lc._pool.acquire() as conn:
        result = await conn.execute(
            'DELETE FROM journal WHERE id = $1 AND owner_id = $2 AND namespace = $3',
            entry_id, target_owner, target_ns,
        )
    if result == 'DELETE 0':
        raise HTTPException(status_code=404, detail="Entry not found")
