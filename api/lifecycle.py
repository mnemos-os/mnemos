"""Shared globals, lifespan, and DB/cache helpers for MNEMOS API."""
import hashlib
import json
import logging
import os
import sys
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import httpx
from fastapi import HTTPException
import redis.asyncio as aioredis

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from config import PG_CONFIG
from inference_backend import get_backend as _get_backend
from graeae.engine import get_graeae_engine  # noqa: F401 — re-exported for handlers

from .models import MemoryItem

logger = logging.getLogger(__name__)

# Compression thresholds
COMPRESSION_RESULT_SET_THRESHOLD = 50 * 1024   # 50 KB
COMPRESSION_ITEM_THRESHOLD = 5 * 1024           # 5 KB per item

# Background task registry — prevents dangling tasks at shutdown
_background_tasks: set = set()

# Worker health tracking
_worker_status: dict = {
    "distillation_worker": "idle",  # idle, healthy, error
    "last_heartbeat": None,
}


def _schedule_background(coro) -> None:
    """Schedule a fire-and-forget coroutine with lifecycle tracking.

    Unlike asyncio.create_task(), tasks created here are tracked in
    _background_tasks so the lifespan teardown can await them before
    closing the DB pool.
    """
    import asyncio as _asyncio
    task = _asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

# DB config sourced from config.PG_CONFIG (env > config.toml > defaults)

# Embedding config (for vector search, MOD-02).
#
# The embedding endpoint is BACKEND-AGNOSTIC. The function _get_embedding
# below auto-detects the wire shape (OpenAI-compat /v1/embeddings vs
# Ollama-compat /api/embeddings), so the same env var works against:
#   - llama.cpp llama-server in embeddings mode (CERBERUS/TYPHON/PYTHIA)
#   - Ollama (legacy / dev workstations)
#   - vLLM with --task embed
#   - NVIDIA NIM embedding containers (e.g. llama-3.2-nv-embedqa-1b-v2)
#   - any OpenAI-compatible /v1/embeddings endpoint
#
# Canonical env vars are INFERENCE_EMBED_* (matches the broader
# _inference_backend namespacing in this module). The OLLAMA_EMBED_*
# names are kept as deprecated fallbacks for backward compat with
# existing .env.prod files; new deployments should use INFERENCE_EMBED_*.
# Deprecation removal scheduled for v4.0 per docs/V4_PLAN.md. The
# OLLAMA_ prefix was a historical artifact — the actual fleet runs
# llama-server, not Ollama, on every host.
_EMBED_HOST = (
    os.getenv('INFERENCE_EMBED_HOST')
    or os.getenv('OLLAMA_EMBED_HOST')
    or 'http://localhost:11434'
)
_EMBED_MODEL = (
    os.getenv('INFERENCE_EMBED_MODEL')
    or os.getenv('OLLAMA_EMBED_MODEL')
    or 'nomic-embed-text'
)
_EMBED_TIMEOUT = float(
    os.getenv('INFERENCE_EMBED_TIMEOUT')
    or os.getenv('OLLAMA_EMBED_TIMEOUT')
    or '10'
)
if os.getenv('OLLAMA_EMBED_HOST') and not os.getenv('INFERENCE_EMBED_HOST'):
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "[EMBED] OLLAMA_EMBED_HOST is deprecated and will be removed in v4.0. "
        "Rename to INFERENCE_EMBED_HOST in your .env. The endpoint is "
        "backend-agnostic — works with llama-server, Ollama, vLLM, NVIDIA "
        "NIM, or any OpenAI-compat embeddings server. The OLLAMA_ prefix "
        "was a historical artifact; the actual fleet runs llama-server, "
        "not Ollama."
    )

# ── Singleton globals ────────────────────────────────────────────────────────
_pool: Optional[asyncpg.Pool] = None
_cache: Optional[aioredis.Redis] = None
_inference_backend = None  # initialized on startup via _get_backend()
_rls_enabled: bool = False   # set from config at startup; read by handlers


def get_inference_backend():
    """Get the distillation backend (Ollama or LlamaCpp)."""
    global _inference_backend
    if _inference_backend is None:
        _inference_backend = _get_backend()
    return _inference_backend


def _load_config() -> dict:
    """Load config.toml from standard locations. Returns empty dict if not found."""
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.toml"),
        "/etc/mnemos/config.toml",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    return tomllib.load(f)
            except Exception as e:
                logger.warning(f"Failed to parse {path}: {e}")
    return {}


# ── Distillation Worker Wrapper ─────────────────────────────────────────────────

async def _run_distillation_worker():
    """Supervised distillation worker loop — restarts on unhandled errors.

    Dispatches unoptimized memories through the v3.3 going-forward
    compression stack:
      - ARTEMIS (CPU-only extractive with identifier preservation)
      - APOLLO  (schema-aware dense encoding, GPU-optional fallback)
    Selected by the contest framework per memory.

    Previously a single crash set status to 'idle' and left the worker
    permanently dead for the rest of the process lifetime. We now restart
    with exponential backoff up to 5 minutes. asyncio.CancelledError (shutdown)
    propagates so the lifespan drain works correctly.
    """
    import asyncio
    global _worker_status

    try:
        from distillation_worker import MemoryDistillationWorker
    except ImportError as e:
        logger.warning(f"Distillation worker not available: {e}")
        _worker_status["distillation_worker"] = "unavailable"
        return

    backoff = 1.0
    while True:
        worker = MemoryDistillationWorker()
        try:
            _worker_status["distillation_worker"] = "starting"
            await worker.start()
            # Graceful exit (worker.start() returned) — stop supervising.
            _worker_status["distillation_worker"] = "idle"
            return
        except asyncio.CancelledError:
            logger.info("Distillation worker cancelled (shutdown)")
            _worker_status["distillation_worker"] = "idle"
            raise
        except Exception as e:
            _worker_status["distillation_worker"] = "error"
            logger.exception(f"Distillation worker crashed: {e} — restarting in {backoff:.0f}s")
        finally:
            try:
                if getattr(worker, "db_pool", None):
                    await worker.db_pool.close()
            except Exception:
                pass
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            _worker_status["distillation_worker"] = "idle"
            raise
        backoff = min(backoff * 2, 300.0)  # cap at 5 minutes


# ── App lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan: initialize and teardown DB pool, Redis, inference provider, and workers."""
    global _pool, _cache, _rls_enabled, _worker_status
    logger.info("Starting MNEMOS API Server v3.0.0 (gateway + sessions + DAG + workers)")

    config = _load_config()

    try:
        _pool = await asyncpg.create_pool(
            user=PG_CONFIG['user'],
            password=PG_CONFIG['password'],
            database=PG_CONFIG['database'],
            host=PG_CONFIG['host'],
            port=PG_CONFIG['port'],
            min_size=PG_CONFIG['pool_min_size'],
            max_size=PG_CONFIG['pool_max_size'],
        )
        app.state.pool = _pool   # auth.py reads this via request.app.state.pool
        logger.info(
            f"asyncpg connection pool initialized "
            f"(min={PG_CONFIG['pool_min_size']}, max={PG_CONFIG['pool_max_size']})"
        )
    except Exception as e:
        logger.error(f"Failed to create DB pool: {e}")
        raise

    # Configure auth (personal profile: auth.enabled=false → no-op beyond singleton)
    from api.auth import configure_auth
    configure_auth(config.get("auth", {}))

    # Refresh GRAEAE provider manifest from model_registry in the background
    # so startup doesn't block on per-provider HTTP probes (each can take up
    # to ~12s with httpx default timeouts; 8 providers × up to 6 candidates
    # would otherwise stall lifespan completion for several minutes on a slow
    # upstream and tie up a DB connection the whole time). The engine starts
    # with the hardcoded _BUILTIN_PROVIDERS defaults and rotates as soon as
    # the background task lands. POST /admin/graeae/reload-providers is also
    # available for on-demand refresh (and is what the daily systemd timer
    # uses after sync_provider_models.py finishes).
    import asyncio as _asyncio_for_reload
    async def _bg_graeae_reload():
        try:
            from graeae.engine import get_graeae_engine
            await _asyncio_for_reload.wait_for(
                get_graeae_engine().reload_from_registry(_pool),
                timeout=120,
            )
        except _asyncio_for_reload.TimeoutError:
            logger.warning(
                "[GRAEAE] background manifest reload exceeded 120s — "
                "keeping built-in defaults; daily timer will retry",
            )
        except Exception as e:
            logger.warning(f"[GRAEAE] background manifest reload failed: {e}")
    _schedule_background(_bg_graeae_reload())

    # RLS enforcement flag
    _rls_enabled = config.get("multiuser", {}).get("rls_enabled", False)
    if _rls_enabled:
        logger.info("Row Level Security: ENABLED (team/enterprise profile)")
    else:
        logger.info("Row Level Security: DISABLED (personal profile)")

    _redis_url = os.getenv("MNEMOS_REDIS_URL") or os.getenv("REDIS_URL") or "redis://localhost:6379"
    try:
        _cache = aioredis.from_url(_redis_url, decode_responses=True)
        await _cache.ping()
        app.state.cache = _cache
        logger.info(f"Redis cache connected ({_redis_url})")
    except Exception as e:
        logger.warning(f"Redis unavailable at {_redis_url}, caching disabled: {e}")
        _cache = None
        app.state.cache = None

    backend = get_inference_backend()
    healthy = await backend.health_check()
    if healthy:
        logger.info("[backend] Distillation backend CONNECTED")
    else:
        logger.warning("[backend] Distillation backend UNREACHABLE - compression disabled")

    # Start background distillation worker (optional)
    worker_enabled = config.get("worker", {}).get("enabled", True)
    if worker_enabled and _pool:
        logger.info("Launching background distillation worker")
        _schedule_background(_run_distillation_worker())
        import asyncio as _asyncio
        await _asyncio.sleep(0.5)  # Give worker time to initialize
    else:
        logger.info("Background distillation worker disabled")
        _worker_status["distillation_worker"] = "disabled"

    # Webhook retry repair + recovery worker. The repair sweep closes the
    # online-upgrade race where an older process committed status=retrying
    # before committing the successor row during the SQL migration window.
    if _pool:
        logger.info('Repairing superseded webhook retry attempts')
        from api.webhook_dispatcher import (  # noqa: WPS433
            recovery_worker_loop as _webhook_recovery,
            repair_superseded_retrying_deliveries as _webhook_retry_repair,
        )
        try:
            repair_result = await _webhook_retry_repair(_pool)
            logger.info('webhook retry repair sweep result: %s', repair_result)
        except Exception:
            logger.exception('webhook retry repair sweep failed')
        logger.info('Launching webhook delivery recovery worker')
        _schedule_background(_webhook_recovery(_pool))

    # Federation sync worker (v3.0.0 — pulls from remote peers on their intervals)
    if _pool:
        logger.info('Launching federation sync worker')
        from api.federation import federation_worker_loop as _federation_worker
        _schedule_background(_federation_worker(_pool))

    # OAuth expired-session GC worker (v3.0.0)
    if _pool:
        import asyncio as _asyncio
        async def _oauth_gc_loop():
            from api.oauth import gc_expired_sessions
            while True:
                try:
                    await _asyncio.sleep(3600)  # hourly
                    deleted = await gc_expired_sessions(_pool)
                    if deleted:
                        logger.info(f'oauth gc: deleted {deleted} expired sessions')
                except _asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception('oauth gc iteration failed')
        _schedule_background(_oauth_gc_loop())

    yield

    if _background_tasks:
        logger.info(f"Draining {len(_background_tasks)} background task(s)…")
        import asyncio as _asyncio
        await _asyncio.gather(*list(_background_tasks), return_exceptions=True)

    if _pool:
        await _pool.close()
        logger.info("DB pool closed")
    if _cache:
        await _cache.aclose()
        logger.info("Redis cache closed")
    await backend.close()
    logger.info("Shutting down MNEMOS API Server")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_cache_key(prefix: str, *args) -> str:
    """Generate a stable, prefixed cache key.

    The namespace prefix ("mnemos:<prefix>:") is preserved so a pattern-based
    invalidation (SCAN MATCH "mnemos:search:*") can target only our keys.

    Args are serialized as a JSON list with stable separators before
    hashing. JSON's quoted strings + escaped delimiters mean two
    distinct argument tuples can never collide on the wire — e.g.
    (category='a:b', subcategory='c') and (category='a',
    subcategory='b:c') were previously both joined to "a:b:c" via
    the old ':'.join encoding and produced the same MD5 digest;
    JSON encoding produces ["a:b","c"] vs ["a","b:c"], distinct
    even before hashing.
    """
    serialized = json.dumps(list(args), separators=(",", ":"), default=str)
    digest = hashlib.md5(serialized.encode(), usedforsecurity=False).hexdigest()
    return f"mnemos:{prefix}:{digest}"


async def _get_db():
    """Acquire a connection from the pool."""
    global _pool
    if not _pool:
        raise HTTPException(status_code=503, detail="Database pool not available")
    return _pool.acquire()


_MEMORY_COLS = (
    "id, content, category, subcategory, created, updated, "
    "metadata, quality_rating, compressed_content, verbatim_content, "
    "owner_id, group_id, namespace, permission_mode, "
    "source_model, source_provider, source_session, source_agent"
)


def _row_to_memory(row, include_compressed: bool = False) -> MemoryItem:
    raw_meta = row.get('metadata')
    if isinstance(raw_meta, str):
        try:
            raw_meta = json.loads(raw_meta)
        except Exception:
            raw_meta = None
    elif not isinstance(raw_meta, dict):
        raw_meta = None
    return MemoryItem(
        id=row['id'],
        content=row['content'],
        category=row['category'],
        subcategory=row.get('subcategory'),
        created=row['created'].isoformat() if row['created'] else '',
        updated=row['updated'].isoformat() if row.get('updated') else None,
        metadata=raw_meta if raw_meta else None,
        quality_rating=row.get('quality_rating'),
        compressed_content=row.get('compressed_content') if include_compressed else None,
        verbatim_content=row.get('verbatim_content'),
        owner_id=row.get('owner_id'),
        group_id=row.get('group_id'),
        namespace=row.get('namespace'),
        permission_mode=row.get('permission_mode'),
        source_model=row.get('source_model'),
        source_provider=row.get('source_provider'),
        source_session=row.get('source_session'),
        source_agent=row.get('source_agent'),
    )


async def _get_embedding(text: str) -> list:
    """Get embedding vector from nomic-embed-text. Returns [] on failure.

    Accepts either wire format from the configured OLLAMA_EMBED_HOST:
      * Ollama-compat /api/embeddings: {"prompt": ...} → {"embedding": [...]}
        (phi_server.py fastembed, Ollama itself)
      * OpenAI-compat /v1/embeddings: {"input": ...} →
        {"data": [{"embedding": [...]}]} (llama.cpp server embeddings mode,
        OpenAI, vLLM)

    Tries OpenAI first (newer; faster llama.cpp SYCL path on PYTHIA),
    falls through to Ollama on 404. Same model (nomic-embed-text-v1.5),
    same 768-dim output — only the wire shape differs.
    """
    truncated = text[:2000]
    try:
        async with httpx.AsyncClient(timeout=_EMBED_TIMEOUT) as client:
            r = await client.post(
                f"{_EMBED_HOST}/v1/embeddings",
                json={"model": _EMBED_MODEL, "input": truncated},
            )
            if r.status_code == 404:
                r = await client.post(
                    f"{_EMBED_HOST}/api/embeddings",
                    json={"model": _EMBED_MODEL, "prompt": truncated},
                )
                r.raise_for_status()
                return r.json().get("embedding", [])
            r.raise_for_status()
            data = r.json().get("data") or []
            if data and isinstance(data[0], dict):
                return data[0].get("embedding", [])
            return []
    except Exception as e:
        logger.warning(f"[EMBED] Failed to get embedding: {e}")
        return []


async def _vector_search(conn, embedding: list, limit: int,
                         category=None, subcategory=None, select_cols=None,
                         source_provider=None, source_model=None,
                         source_agent=None, namespace=None,
                         owner_id=None, group_ids=None) -> list:
    """pgvector cosine similarity search. Returns rows ordered by similarity desc.

    The vector is always $1 — used in both the SELECT similarity expression and
    the ORDER BY clause.  Passing it as a parameter (not interpolated into the
    query string) eliminates any injection risk from a poisoned embedding response.
    Supports optional provenance filters (source_provider, source_model,
    source_agent, namespace) ANDed into the WHERE clause.

    When the caller passes ``owner_id`` AND ``group_ids`` (a list, possibly
    empty), the visibility predicate is the full v1_multiuser-mirror from
    api.visibility — owner / federation / world-readable / group-readable.
    Non-root callers from /memories/search pin owner_id=user.user_id and
    pass group_ids=user.group_ids; the same predicate is used by list/get,
    so a memory visible to one read path is visible to all.

    Backward-compat: callers that pass owner_id alone (without group_ids)
    keep the older (owner_id OR federation_source) shape — used for any
    legacy call sites that haven't been audited yet.
    """
    if select_cols is None:
        select_cols = _MEMORY_COLS
    # float() cast guards against non-numeric values in the embedding response
    vec_str = "[" + ",".join(str(float(x)) for x in embedding) + "]"
    # $1 is the vector — referenced in SELECT and ORDER BY, never interpolated
    sim_col = "1 - (embedding <=> $1::vector) AS similarity"

    # Dynamic WHERE builder: $1=vec_str, filter params at $2+, limit always last
    params: list = [vec_str]
    conditions: list = ["embedding IS NOT NULL"]
    for col, val in [("category", category), ("subcategory", subcategory),
                     ("source_provider", source_provider), ("source_model", source_model),
                     ("source_agent", source_agent), ("namespace", namespace)]:
        if val is not None:
            params.append(val)
            conditions.append(f"{col}=${len(params)}")
    if owner_id is not None:
        if group_ids is not None:
            # Full v1_multiuser-mirror predicate via the shared module.
            from api.visibility import read_visibility_predicate
            clause, vis_params = read_visibility_predicate(
                owner_id, list(group_ids), len(params) + 1,
            )
            conditions.append(clause)
            params.extend(vis_params)
        else:
            # Legacy backward-compat: owner-or-federation only.
            params.append(owner_id)
            conditions.append(
                f"(owner_id=${len(params)} OR federation_source IS NOT NULL)"
            )
    params.append(limit)
    limit_ph = f"${len(params)}"

    where = " AND ".join(conditions)
    sql = (f"SELECT {select_cols}, {sim_col} FROM memories "
           f"WHERE {where} ORDER BY embedding <=> $1::vector LIMIT {limit_ph}")
    try:
        return await conn.fetch(sql, *params)
    except Exception as e:
        logger.error(f"[VECTOR] pgvector search failed: {e}")
        return []


async def _fts_fetch(conn, query: str, limit: int,
                     category=None, subcategory=None, select_cols=None,
                     source_provider=None, source_model=None,
                     source_agent=None, namespace=None,
                     owner_id=None, group_ids=None):
    """FTS search with ILIKE fallback. Shared by /memories/search and /memories/rehydrate.

    Uses plainto_tsquery (not to_tsquery) so user input is treated as plain text —
    tsquery operators like |, !, & are not interpreted.  This prevents tsquery
    operator injection while preserving full-text search quality.
    Supports optional provenance filters (source_provider, source_model,
    source_agent, namespace) ANDed into the WHERE clause.

    When the caller passes ``owner_id`` AND ``group_ids``, the visibility
    predicate is the full v1_multiuser-mirror — owner / federation /
    world-readable / group-readable. Same shape used by list/get; consistent
    across all read paths so a row visible via one path is visible via all.
    Backward-compat: passing owner_id alone keeps the older (owner_id OR
    federation_source) shape.
    """
    if select_cols is None:
        select_cols = _MEMORY_COLS
    clean_query = query.strip()
    rank_col = "ts_rank(to_tsvector('english', content), plainto_tsquery('english', $1)) as rank"

    def _build_filters(start_params: list) -> tuple[list, list]:
        """Append provenance + visibility conditions to start_params.
        Returns (conditions, params) so callers can add their own
        leading conditions (e.g. the FTS match).
        """
        params = list(start_params)
        conditions: list = []
        for col, val in [("category", category), ("subcategory", subcategory),
                         ("source_provider", source_provider), ("source_model", source_model),
                         ("source_agent", source_agent), ("namespace", namespace)]:
            if val is not None:
                params.append(val)
                conditions.append(f"{col}=${len(params)}")
        if owner_id is not None:
            if group_ids is not None:
                from api.visibility import read_visibility_predicate
                clause, vis_params = read_visibility_predicate(
                    owner_id, list(group_ids), len(params) + 1,
                )
                conditions.append(clause)
                params.extend(vis_params)
            else:
                params.append(owner_id)
                conditions.append(
                    f"(owner_id=${len(params)} OR federation_source IS NOT NULL)"
                )
        return conditions, params

    # FTS path: $1=query, $2=limit; filter params at $3+
    fts_conditions, fts_params = _build_filters([clean_query, limit])
    fts_conditions = ["to_tsvector('english', content) @@ plainto_tsquery('english', $1)"] + fts_conditions
    where = " AND ".join(fts_conditions)
    sql = (f"SELECT {select_cols}, {rank_col} FROM memories "
           f"WHERE {where} ORDER BY rank DESC LIMIT $2")
    try:
        return await conn.fetch(sql, *fts_params)
    except Exception:
        logger.warning(f"[FTS] falling back to ILIKE for: {query[:50]!r}")
        like_q = f"%{query}%"
        # ILIKE path: $1=like_q, $2=limit; filter params at $3+
        ilike_conditions, ilike_params = _build_filters([like_q, limit])
        ilike_conditions = ["content ILIKE $1"] + ilike_conditions
        ilike_where = " AND ".join(ilike_conditions)
        ilike_sql = (f"SELECT {select_cols} FROM memories "
                     f"WHERE {ilike_where} ORDER BY created DESC LIMIT $2")
        try:
            return await conn.fetch(ilike_sql, *ilike_params)
        except Exception as e2:
            logger.error(f"[FTS] Both FTS and ILIKE failed: {e2}")
            return []
