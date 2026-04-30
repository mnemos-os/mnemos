"""Session ingestion endpoint."""
import logging

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.api.routes.memories import _insert_memory_with_created_webhook, _rls_context
from mnemos.core.ids import new_memory_id
from mnemos.domain.models import SessionIngestRequest, SessionIngestResponse

logger = logging.getLogger(__name__)
router = APIRouter()


def _extract_readable(items: list, max_items: int = 20) -> str:
    """Extract human-readable text from session items.

    Handles common message formats ({role, content}, {type, text}, plain strings).
    Caps at max_items to prevent unbounded memory growth.
    Never calls str() on arbitrary objects — only extracts validated string fields.
    """
    parts = []
    for item in items[:max_items]:
        if isinstance(item, dict):
            content = item.get("content") or item.get("text") or item.get("body") or ""
            if not isinstance(content, str):
                continue
            role = item.get("role") or item.get("type") or ""
            parts.append(f"[{role}] {content[:500]}" if role else content[:500])
        elif isinstance(item, str):
            parts.append(item[:500])
    return "\n".join(parts) if parts else "(no readable content)"




@router.post("/ingest/session", response_model=SessionIngestResponse)
async def ingest_session(request: SessionIngestRequest, user: UserContext = Depends(get_current_user)):
    """Ingest Claude Code session data into MNEMOS."""
    if not _lc._pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    stored_ids = []
    try:
        data = request.raw_data
        async with _lc.get_pool_manager().acquire() as conn:
            nats_intents = []
            async with _rls_context(conn, user):
                async with conn.transaction():
                    for key, fallback_key, item_type, label, category in (
                        ("messages", "prompts", "messages", "messages", "session_activity"),
                        ("code_blocks", None, "code", "code blocks", "session_code"),
                        ("tool_operations", "tools", "tools", "tool operations", "session_tools"),
                    ):
                        items = data.get(key, [])
                        if not items and fallback_key:
                            items = data.get(fallback_key, [])
                        if not items:
                            continue

                        content = (
                            f"Session {request.session_id} — {len(items)} {label}\n"
                            f"{_extract_readable(items)}"
                        )
                        mem_id = new_memory_id()
                        metadata = {
                            "source": request.source,
                            "session_id": request.session_id,
                            "machine_id": request.machine_id,
                            "agent_id": request.agent_id,
                            "git_commit": request.git_commit,
                            "item_count": len(items),
                            "item_type": item_type,
                        }
                        nats_intents.extend(await _insert_memory_with_created_webhook(
                            conn=conn,
                            mem_id=mem_id,
                            content=content,
                            category=category,
                            subcategory=None,
                            metadata=metadata,
                            owner_id=user.user_id,
                            namespace=user.namespace,
                            permission_mode=600,
                            verbatim_content=content,
                            source_model=None,
                            source_provider=request.source,
                            source_session=request.session_id,
                            source_agent=request.agent_id,
                        ))
                        stored_ids.append(mem_id)

        from mnemos.nats import publish_event as _nats_publish_event
        for subject, payload, msg_id in nats_intents:
            await _nats_publish_event(subject, payload, msg_id=msg_id)

        if _lc._cache:
            try:
                await _lc._cache.delete("stats:global")
            except Exception:
                pass

        logger.info(f"Session {request.session_id} ingested: {len(stored_ids)} records")
        return SessionIngestResponse(
            success=True, session_id=request.session_id,
            stored_count=len(stored_ids), memory_ids=stored_ids,
        )
    except asyncpg.PostgresError as e:
        logger.error(f"Session ingestion DB error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    except Exception as e:
        logger.error(f"Session ingestion failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
