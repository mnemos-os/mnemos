"""Shared globals, lifespan, and DB/cache helpers for MNEMOS API."""
import hashlib
import ipaddress
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
from urllib.parse import urlparse

import asyncpg
import httpx
import redis.asyncio as aioredis
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
from mnemos.core.config import PG_CONFIG, get_settings
from mnemos.core.pool import PoolManager

logger = logging.getLogger(__name__)

# Background task registries — keep finite webhook sends out of the cancel-first
# worker pool so graceful shutdown can let them finalize their leases.
_background_tasks: set = set()
_worker_tasks: set = set()
_delivery_attempt_tasks: set = set()
_WORKER_SHUTDOWN_CANCEL_SECONDS = get_settings().runtime.worker_shutdown_cancel_seconds
_FINAL_CANCEL_WAIT_SECONDS = 5.0


WEBHOOK_SHUTDOWN_DRAIN_SECONDS = float(get_settings().webhook.shutdown_drain_seconds)

# Worker health tracking
_worker_status: dict = {
    "distillation_worker": "idle",  # idle, healthy, error
    "last_heartbeat": None,
}


def _schedule_tracked(coro, registry: set):
    import asyncio as _asyncio
    task = _asyncio.create_task(coro)
    registry.add(task)
    task.add_done_callback(registry.discard)
    return task


def _schedule_background(coro):
    """Schedule a generic finite background task with lifecycle tracking."""
    return _schedule_tracked(coro, _background_tasks)


def _schedule_worker(coro):
    """Schedule a perpetual worker loop that shutdown cancels in phase 1."""
    return _schedule_tracked(coro, _worker_tasks)


def _schedule_delivery_attempt(coro):
    """Schedule a finite webhook send that graceful shutdown drains before cancel."""
    return _schedule_tracked(coro, _delivery_attempt_tasks)


_auth_configurer = None
_provider_manifest_reloader = None
_lifespan_worker_factories: dict = {}


def register_auth_configurer(configurer) -> None:
    """Register the API-owned auth configurer without making core import API."""
    global _auth_configurer
    _auth_configurer = configurer


def register_provider_manifest_reloader(reloader) -> None:
    """Register the domain-owned provider manifest reload hook."""
    global _provider_manifest_reloader
    _provider_manifest_reloader = reloader


def register_lifespan_worker(name: str, factory, *, honor_worker_enabled: bool = False) -> None:
    """Register an app worker factory called with the lifecycle DB pool."""
    _lifespan_worker_factories[name] = (factory, honor_worker_enabled)


async def _run_distillation_worker(pool=None):
    """Compatibility shim for the registered distillation worker supervisor."""
    factory_entry = _lifespan_worker_factories.get("distillation_worker")
    if factory_entry is None:
        _worker_status["distillation_worker"] = "unavailable"
        return
    factory, _honor_worker_enabled = factory_entry
    await factory(pool)


async def _cancel_tracked_tasks(tasks: set, *, label: str, timeout: float) -> None:
    if not tasks:
        return

    import asyncio as _asyncio

    snapshot = list(tasks)
    logger.info(f"Cancelling {len(snapshot)} {label} task(s)…")
    for task in snapshot:
        task.cancel()

    done, pending = await _asyncio.wait(snapshot, timeout=timeout)
    if done:
        await _asyncio.gather(*done, return_exceptions=True)

    if pending:
        logger.warning(
            "%d %s task(s) did not stop within %.1fs; continuing shutdown",
            len(pending),
            label,
            timeout,
        )
        for task in pending:
            task.cancel()
        stopped, still_pending = await _asyncio.wait(pending, timeout=_FINAL_CANCEL_WAIT_SECONDS)
        if stopped:
            await _asyncio.gather(*stopped, return_exceptions=True)
        if still_pending:
            logger.error(
                "%d %s task(s) ignored cancellation for another %.1fs",
                len(still_pending),
                label,
                _FINAL_CANCEL_WAIT_SECONDS,
            )


async def _drain_delivery_attempt_tasks() -> None:
    if not _delivery_attempt_tasks:
        return

    import asyncio as _asyncio

    tasks = list(_delivery_attempt_tasks)
    logger.info(
        "Waiting up to %.1fs for %d webhook delivery attempt task(s) to finalize",
        WEBHOOK_SHUTDOWN_DRAIN_SECONDS,
        len(tasks),
    )
    done, pending = await _asyncio.wait(tasks, timeout=WEBHOOK_SHUTDOWN_DRAIN_SECONDS)
    if done:
        await _asyncio.gather(*done, return_exceptions=True)

    if pending:
        logger.error(
            "Cancelling %d webhook delivery attempt task(s) after %.1fs drain; "
            "these deliveries may replay on restart if the HTTP side effect already happened",
            len(pending),
            WEBHOOK_SHUTDOWN_DRAIN_SECONDS,
        )
        for task in pending:
            task.cancel()
        stopped, still_pending = await _asyncio.wait(pending, timeout=_FINAL_CANCEL_WAIT_SECONDS)
        if stopped:
            await _asyncio.gather(*stopped, return_exceptions=True)
        if still_pending:
            logger.error(
                "%d webhook delivery attempt task(s) ignored last-resort cancellation for %.1fs",
                len(still_pending),
                _FINAL_CANCEL_WAIT_SECONDS,
            )

# DB config sourced from config.PG_CONFIG (env > config.toml > defaults)

# Embedding config (for vector search, MOD-02).
#
# The embedding endpoint is BACKEND-AGNOSTIC. The function _get_embedding
# below auto-detects the wire shape (OpenAI-compat /v1/embeddings vs
# Ollama-compat /api/embeddings), so the same env var works against:
#   - llama.cpp llama-server in embeddings mode (CERBERUS/TYPHON/PYTHIA)
#   - Ollama (dev workstations)
#   - vLLM with --task embed
#   - NVIDIA NIM embedding containers (e.g. llama-3.2-nv-embedqa-1b-v2)
#   - any OpenAI-compatible /v1/embeddings endpoint
#
# Canonical env vars are INFERENCE_EMBED_* so embedding config is not
# tied to any one inference server implementation.
_PROVIDER_SETTINGS = get_settings().providers
_EMBED_HOST = _PROVIDER_SETTINGS.inference_embed_host
_EMBED_MODEL = _PROVIDER_SETTINGS.inference_embed_model
_EMBED_TIMEOUT = _PROVIDER_SETTINGS.inference_embed_timeout

# ── Singleton globals ────────────────────────────────────────────────────────
_pool: Optional[asyncpg.Pool] = None
_pool_manager: Optional[PoolManager] = None
_cache: Optional[aioredis.Redis] = None
_rls_enabled: bool = False   # set from config at startup; read by handlers


def _load_config() -> dict:
    """Load config.toml from standard locations. Returns empty dict if not found."""
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.toml"),
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


def _configured_federation_peer_urls() -> list[str]:
    raw = get_settings().federation.peers.strip()
    if not raw:
        return []

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = None

    if isinstance(decoded, list):
        urls: list[str] = []
        for item in decoded:
            if isinstance(item, str):
                urls.append(item.strip())
            elif isinstance(item, dict):
                url = item.get("base_url") or item.get("url")
                if isinstance(url, str):
                    urls.append(url.strip())
        return [url for url in urls if url]

    urls = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        if "=" in token:
            token = token.split("=", 1)[1].strip()
        urls.append(token)
    return urls


def _peer_url_looks_same_lan(url: str) -> bool:
    parsed = urlparse(url if "://" in url else f"//{url}")
    hostname = parsed.hostname
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        peer_ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return peer_ip.is_private or peer_ip.is_loopback or peer_ip.is_link_local


async def _log_federation_startup_guidance(pool: asyncpg.Pool) -> None:
    configured_peer_urls = _configured_federation_peer_urls()
    db_peer_urls: list[str] = []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT base_url FROM federation_peers WHERE enabled")
        db_peer_urls = [row["base_url"] for row in rows if row["base_url"]]
    except Exception as exc:
        logger.debug("federation startup guidance skipped DB peer scan: %s", exc)

    if get_settings().federation.enabled and not configured_peer_urls and not db_peer_urls:
        logger.info("federation enabled but no peers configured — federation pulls and exports are inactive.")

    for peer_url in [*configured_peer_urls, *db_peer_urls]:
        if _peer_url_looks_same_lan(peer_url):
            logger.warning(
                "federation peer %s appears to be same-LAN; for single-site HA, "
                "Postgres streaming replication is faster and simpler — see DEPLOYMENT.md",
                peer_url,
            )


# ── App lifespan ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    """FastAPI lifespan: initialize and teardown DB pool, Redis, and workers."""
    global _pool, _pool_manager, _cache, _rls_enabled, _worker_status
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
        _pool_manager = PoolManager(_pool)
        app.state.pool = _pool   # auth.py reads this via request.app.state.pool
        app.state.pool_manager = _pool_manager
        logger.info(
            f"asyncpg connection pool initialized "
            f"(min={PG_CONFIG['pool_min_size']}, max={PG_CONFIG['pool_max_size']})"
        )
    except Exception as e:
        logger.error(f"Failed to create DB pool: {e}")
        raise

    # Configure auth (personal profile: auth.enabled=false -> no-op beyond singleton).
    if _auth_configurer is not None:
        _auth_configurer(config.get("auth", {}))

    # Refresh GRAEAE provider manifest from model_registry in the background
    # so startup doesn't block on per-provider HTTP probes (each can take up
    # to ~12s with httpx default timeouts; 8 providers × up to 6 candidates
    # would otherwise stall lifespan completion for several minutes on a slow
    # upstream and tie up a DB connection the whole time). The engine starts
    # with the hardcoded _BUILTIN_PROVIDERS defaults and rotates as soon as
    # the background task lands. POST /admin/graeae/reload-providers is also
    # available for on-demand refresh (and is what the daily systemd timer
    # uses after sync_provider_models.py finishes).
    if _provider_manifest_reloader is not None:
        import asyncio as _asyncio_for_reload

        async def _bg_provider_manifest_reload():
            try:
                await _asyncio_for_reload.wait_for(
                    _provider_manifest_reloader(_pool),
                    timeout=120,
                )
            except _asyncio_for_reload.TimeoutError:
                logger.warning(
                    "[GRAEAE] background manifest reload exceeded 120s - "
                    "keeping built-in defaults; daily timer will retry",
                )
            except Exception as e:
                logger.warning(f"[GRAEAE] background manifest reload failed: {e}")

        _schedule_background(_bg_provider_manifest_reload())

    # RLS enforcement flag
    _rls_enabled = config.get("multiuser", {}).get("rls_enabled", False)
    if _rls_enabled:
        logger.info("Row Level Security: ENABLED (team/enterprise profile)")
    else:
        logger.info("Row Level Security: DISABLED (personal profile)")

    await _log_federation_startup_guidance(_pool)

    _redis_url = get_settings().server.redis_url
    try:
        _cache = aioredis.from_url(_redis_url, decode_responses=True)
        await _cache.ping()
        app.state.cache = _cache
        logger.info(f"Redis cache connected ({_redis_url})")
    except Exception as e:
        logger.warning(f"Redis unavailable at {_redis_url}, caching disabled: {e}")
        _cache = None
        app.state.cache = None

    # Start registered background workers. API owns registrations so core stays
    # below API/domain/webhook/worker packages in the dependency graph.
    worker_enabled = config.get("worker", {}).get("enabled", True)
    scheduled_workers = 0
    if _pool:
        for worker_name, (factory, honor_worker_enabled) in _lifespan_worker_factories.items():
            if honor_worker_enabled and not worker_enabled:
                logger.info("%s disabled", worker_name)
                if worker_name == "distillation_worker":
                    _worker_status["distillation_worker"] = "disabled"
                continue
            logger.info("Launching %s", worker_name)
            _schedule_worker(factory(_pool))
            scheduled_workers += 1
    if scheduled_workers:
        import asyncio as _asyncio
        await _asyncio.sleep(0.5)  # Give worker time to initialize
    elif not worker_enabled:
        logger.info("Background distillation worker disabled")
        _worker_status["distillation_worker"] = "disabled"

    # OAuth expired-session GC worker (v3.0.0)
    if _pool:
        import asyncio as _asyncio
        async def _oauth_gc_loop():
            from mnemos.core.oauth import gc_expired_sessions
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
        _schedule_worker(_oauth_gc_loop())

    yield

    await _cancel_tracked_tasks(
        _worker_tasks,
        label="worker",
        timeout=_WORKER_SHUTDOWN_CANCEL_SECONDS,
    )
    await _drain_delivery_attempt_tasks()
    await _cancel_tracked_tasks(
        _background_tasks,
        label="background",
        timeout=_WORKER_SHUTDOWN_CANCEL_SECONDS,
    )

    if _pool:
        await _pool.close()
        logger.info("DB pool closed")
    _pool_manager = None
    if _cache:
        await _cache.aclose()
        logger.info("Redis cache closed")
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


def get_pool_manager() -> PoolManager:
    """Return the lifecycle-owned pool manager singleton."""
    global _pool_manager
    if _pool_manager is None or (_pool is not None and _pool_manager.pool is not _pool):
        if not _pool:
            raise HTTPException(status_code=503, detail="Database pool not available")
        _pool_manager = PoolManager(_pool)
    return _pool_manager


_MEMORY_COLS = (
    "id, content, category, subcategory, created, updated, "
    "metadata, quality_rating, compressed_content, verbatim_content, "
    "owner_id, group_id, namespace, permission_mode, "
    "source_model, source_provider, source_session, source_agent"
)


async def _get_embedding(text: str) -> list:
    """Get embedding vector from nomic-embed-text. Returns [] on failure.

    Accepts either wire format from the configured INFERENCE_EMBED_HOST:
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

    When the caller passes ``owner_id``, the visibility predicate is the
    full v1_multiuser-mirror from mnemos.core.visibility — owner / federation /
    world-readable / group-readable.
    Non-root callers from /memories/search pin owner_id=user.user_id and
    pass group_ids=user.group_ids; the same predicate is used by list/get,
    so a memory visible to one read path is visible to all.

    ``group_ids`` may be an empty list; omitted group_ids are treated as
    no group memberships.
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
        from mnemos.core.visibility import read_visibility_predicate
        clause, vis_params = read_visibility_predicate(
            owner_id, list(group_ids or []), len(params) + 1,
        )
        conditions.append(clause)
        params.extend(vis_params)
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

    When the caller passes ``owner_id``, the visibility predicate is the
    full v1_multiuser-mirror — owner / federation / world-readable /
    group-readable. Same shape used by list/get; consistent across all read
    paths so a row visible via one path is visible via all. ``group_ids`` may
    be an empty list; omitted group_ids are treated as no group memberships.
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
            from mnemos.core.visibility import read_visibility_predicate
            clause, vis_params = read_visibility_predicate(
                owner_id, list(group_ids or []), len(params) + 1,
            )
            conditions.append(clause)
            params.extend(vis_params)
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
