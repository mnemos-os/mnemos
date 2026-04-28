"""Session management endpoints for stateful multi-turn conversations.

Sessions store conversation history server-side, accumulate memory context across turns,
and track compression/cost metrics per session. Integrates with gateway memory injection
and context routing.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Query

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user
from api.models import (
    SessionRequest, SessionResponse, SessionMessage, SessionMessageResponse,
    SessionHistoryResponse, ChatMessage, SessionContext
)
from api.handlers.openai_compat import (
    _search_mnemos_context, _route_to_provider
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


def _require_pool():
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    return _lc._pool


def _scope_namespace(user: UserContext, override: Optional[str] = None) -> str:
    if override and override != user.namespace:
        if user.role != "root":
            raise HTTPException(
                status_code=403,
                detail="cross-namespace access requires root",
            )
        return override
    return user.namespace


@router.post("", response_model=SessionResponse)
async def create_session(
    request: SessionRequest,
    user: UserContext = Depends(get_current_user),
):
    """Create a new session for multi-turn conversation."""
    pool = _require_pool()

    session_id = None
    try:
        async with pool.acquire() as conn:
            session_id = await conn.fetchval(
                """
                INSERT INTO sessions (user_id, namespace, model)
                VALUES ($1, $2, $3)
                RETURNING id
                """,
                user.user_id,
                user.namespace,
                request.model or "gpt-4o",
            )

            # Optionally add initial system context
            if request.initial_context:
                await conn.execute(
                    """
                    INSERT INTO session_messages (session_id, role, content)
                    VALUES ($1, 'system', $2)
                    """,
                    session_id,
                    request.initial_context,
                )

        logger.info(f"[SESSIONS] Created session {session_id} for user {user.user_id}")

        # Return session metadata
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, created_at, model FROM sessions "
                "WHERE id = $1 AND user_id = $2 AND namespace = $3",
                session_id, user.user_id, user.namespace,
            )

        return SessionResponse(
            session_id=row["id"],
            created_at=row["created_at"].isoformat(),
            model=row["model"],
        )

    except Exception as e:
        logger.error(f"[SESSIONS] Failed to create session: {e}")
        raise HTTPException(status_code=500, detail=f"Session creation failed: {str(e)}")


@router.get("/{session_id}", response_model=SessionContext)
async def get_session(
    session_id: str,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Get session context and metadata."""
    pool = _require_pool()
    target_ns = _scope_namespace(user, namespace)

    async with pool.acquire() as conn:
        session = await conn.fetchrow(
            "SELECT * FROM sessions WHERE id = $1 AND user_id = $2 AND namespace = $3",
            session_id,
            user.user_id,
            target_ns,
        )

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Get list of injected memory IDs for this session
    async with pool.acquire() as conn:
        injections = await conn.fetch(
            """
            SELECT memory_id FROM session_memory_injections
            WHERE session_id = $1
            GROUP BY memory_id
            ORDER BY MAX(injection_timestamp) DESC
            LIMIT 10
            """,
            session_id,
        )

    return SessionContext(
        session_id=session["id"],
        user_id=session["user_id"],
        created_at=session["created_at"].isoformat(),
        last_activity=session["last_activity"].isoformat(),
        message_count=session["message_count"],
        total_tokens=session["total_tokens"],
        model=session["model"],
        injected_memories=[m["memory_id"] for m in injections],
    )


@router.post("/{session_id}/messages", response_model=SessionMessageResponse)
async def add_session_message(
    session_id: str,
    request: SessionMessage,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Add message to session, search memory, inject context, call provider, return response.

    This is the main stateful chat endpoint. It:
    1. Stores user message in history
    2. Searches MNEMOS for relevant context
    3. Injects bounded memory snippets into system prompt
    4. Routes to provider with accumulated context
    5. Stores assistant response in history
    6. Updates session metrics
    """
    pool = _require_pool()
    target_ns = _scope_namespace(user, namespace)

    # Verify session ownership
    async with pool.acquire() as conn:
        session = await conn.fetchrow(
            "SELECT * FROM sessions WHERE id = $1 AND user_id = $2 AND namespace = $3",
            session_id,
            user.user_id,
            target_ns,
        )

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Store user message
    message_id = None
    async with pool.acquire() as conn:
        message_id = await conn.fetchval(
            """
            INSERT INTO session_messages (session_id, role, content, model)
            VALUES ($1, $2, $3, $4)
            RETURNING id
            """,
            session_id,
            request.role or "user",
            request.content,
            request.model or session["model"],
        )

    # Get conversation history for the provider:
    #   1. The earliest role='system' row (the initial_context written
    #      at create_session time) — ALWAYS included, never evictable.
    #   2. Up to 4 most recent later system rows (subsequent policy
    #      updates posted via add_session_message). Bounded to prevent
    #      adversarial role='system' spam from blowing the prompt
    #      budget.
    #   3. The 10 most recent non-system messages, chronological.
    #
    # Iteration history under Codex review:
    #   - ASC LIMIT 10  → returned 10 OLDEST messages (wrong dir).
    #   - DESC LIMIT 10 → recent works, but loses initial_context.
    #   - LIMIT 1 pinned earliest → drops later system updates.
    #   - No LIMIT pinned → unbounded pinned context.
    #   - LIMIT 5 pinned (most recent) → 5 later system writes
    #     evict the foundational initial_context (adversarial path).
    #   - This shape: earliest pinned + 4 most recent later +
    #     10 recent non-system. Initial context never evictable;
    #     later updates capped; bounded total surface.
    #
    # Token-aware truncation and privilege-gating role='system' on
    # add_session_message remain the structurally correct redesign;
    # tracked separately, out of scope here.
    # Ordering key is (timestamp, id) — pure timestamp is not unique
    # in session_messages, so an exclusion that uses `timestamp >`
    # alone could either double-count the initial row or skip it on
    # a tie. Using the row id as a tie-breaker is deterministic.
    async with pool.acquire() as conn:
        history = await conn.fetch(
            """
            WITH first_system AS (
                SELECT id, role, content, timestamp
                  FROM session_messages
                 WHERE session_id = $1 AND role = 'system'
                 ORDER BY timestamp ASC, id ASC
                 LIMIT 1
            ),
            later_system AS (
                SELECT s.id, s.role, s.content, s.timestamp
                  FROM session_messages s
                 WHERE s.session_id = $1
                   AND s.role = 'system'
                   AND s.id <> (SELECT id FROM first_system)
                 ORDER BY s.timestamp DESC, s.id DESC
                 LIMIT 4
            ),
            pinned AS (
                SELECT id, role, content, timestamp, 0 AS k FROM first_system
                UNION ALL
                SELECT id, role, content, timestamp, 0 AS k FROM later_system
            ),
            recent AS (
                SELECT id, role, content, timestamp, 1 AS k
                  FROM session_messages
                 WHERE session_id = $1 AND role <> 'system'
                 ORDER BY timestamp DESC, id DESC
                 LIMIT 10
            )
            SELECT role, content FROM (
                SELECT * FROM pinned
                UNION ALL
                SELECT * FROM recent
            ) all_msgs
            ORDER BY k, timestamp ASC, id ASC
            """,
            session_id,
        )

    # Search MNEMOS for context
    memories_injected = 0
    mnemos_context = ""

    try:
        mnemos_docs = await _search_mnemos_context(request.content, user, limit=3)

        if mnemos_docs:
            # Store injection record for each memory.
            async with pool.acquire() as conn:
                for i, doc in enumerate(mnemos_docs):
                    memory_id = doc.get("id", f"doc_{i}")
                    await conn.execute(
                        """
                        INSERT INTO session_memory_injections
                        (session_id, message_id, memory_id, relevance_score)
                        VALUES ($1, $2, $3, $4)
                        """,
                        session_id,
                        message_id,
                        memory_id,
                        0.9 - (i * 0.1),  # decreasing relevance
                    )

            mnemos_context = "\n\n".join([f"[Memory]\n{doc['content'][:500]}" for doc in mnemos_docs])
            memories_injected = len(mnemos_docs)
            [doc.get("id") for doc in mnemos_docs]

            logger.info(f"[SESSIONS] Injected {memories_injected} memories into session {session_id}")

    except Exception as e:
        logger.warning(f"[SESSIONS] Memory search failed: {e}, continuing without context")

    # Build messages for provider (include session history + injected context)
    messages = [
        {"role": msg["role"], "content": msg["content"]}
        for msg in history
    ]

    # Add system prompt with MNEMOS context if available
    system_prompt = ""
    has_system = any(m["role"] == "system" for m in messages)

    if mnemos_context:
        system_prompt = f"[MNEMOS Context - {memories_injected} memories]\n{mnemos_context}"
        if has_system:
            # Append to existing system prompt
            messages[0]["content"] += f"\n\n{system_prompt}"
        else:
            messages.insert(0, {"role": "system", "content": system_prompt})

    # Route to provider
    model = request.model or session["model"]
    response_text = ""
    tokens_used = 0

    try:
        response_text = await _route_to_provider(
            model=model,
            messages=messages,
            temperature=0.7,
            max_tokens=None,
            user=user,
        )

        # Estimate tokens (rough approximation: ~4 chars per token)
        tokens_used = len(response_text) // 4

    except Exception as e:
        logger.error(f"[SESSIONS] Provider routing failed: {e}")
        raise HTTPException(status_code=503, detail=f"Provider unavailable: {str(e)}")

    # Store assistant response
    assistant_message_id = None
    async with pool.acquire() as conn:
        assistant_message_id = await conn.fetchval(
            """
            INSERT INTO session_messages
            (session_id, role, content, model, tokens_used, memories_injected)
            VALUES ($1, 'assistant', $2, $3, $4, $5)
            RETURNING id
            """,
            session_id,
            response_text,
            model,
            tokens_used,
            memories_injected,
        )

        # Update session metrics
        await conn.execute(
            """
            UPDATE sessions
            SET message_count = message_count + 2,
                total_tokens = total_tokens + $2,
                last_activity = NOW()
            WHERE id = $1 AND user_id = $3 AND namespace = $4
            """,
            session_id,
            tokens_used,
            user.user_id,
            target_ns,
        )

    logger.info(
        f"[SESSIONS] Added message to session {session_id}: "
        f"user→assistant, {tokens_used} tokens, {memories_injected} memories"
    )

    return SessionMessageResponse(
        session_id=session_id,
        message_id=assistant_message_id,
        role="assistant",
        content=response_text,
        model=model,
        timestamp=datetime.now(timezone.utc).isoformat(),
        tokens_used=tokens_used,
        memories_injected=memories_injected,
    )


@router.get("/{session_id}/history", response_model=SessionHistoryResponse)
async def get_session_history(
    session_id: str,
    limit: int = 50,
    offset: int = 0,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Get conversation history for session."""
    pool = _require_pool()
    target_ns = _scope_namespace(user, namespace)

    # Verify session ownership
    async with pool.acquire() as conn:
        session = await conn.fetchrow(
            "SELECT * FROM sessions WHERE id = $1 AND user_id = $2 AND namespace = $3",
            session_id,
            user.user_id,
            target_ns,
        )

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Get paginated message history
    async with pool.acquire() as conn:
        messages = await conn.fetch(
            """
            SELECT role, content, timestamp, model FROM session_messages
            WHERE session_id = $1
            ORDER BY timestamp ASC
            LIMIT $2 OFFSET $3
            """,
            session_id,
            limit,
            offset,
        )

        total = await conn.fetchval(
            "SELECT COUNT(*) FROM session_messages WHERE session_id = $1",
            session_id,
        )

    return SessionHistoryResponse(
        session_id=session_id,
        messages=[
            ChatMessage(
                role=m["role"],
                content=m["content"],
                timestamp=m["timestamp"].isoformat() if m["timestamp"] else None,
                model=m["model"],
            )
            for m in messages
        ],
        total_messages=total,
        total_tokens=session["total_tokens"],
        created_at=session["created_at"].isoformat(),
    )


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    namespace: Optional[str] = Query(None),
    user: UserContext = Depends(get_current_user),
):
    """Close and delete session."""
    pool = _require_pool()
    target_ns = _scope_namespace(user, namespace)

    # Verify session ownership
    async with pool.acquire() as conn:
        session = await conn.fetchrow(
            "SELECT id FROM sessions WHERE id = $1 AND user_id = $2 AND namespace = $3",
            session_id,
            user.user_id,
            target_ns,
        )

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Delete session (cascade deletes messages and injections)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM sessions WHERE id = $1 AND user_id = $2 AND namespace = $3",
            session_id, user.user_id, target_ns,
        )

    logger.info(f"[SESSIONS] Deleted session {session_id}")

    return {"status": "deleted", "session_id": session_id}
