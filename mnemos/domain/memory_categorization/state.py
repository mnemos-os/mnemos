"""
StateManager: Key-value session state backed by PostgreSQL.

Provides:
- get(key): Load value for key
- set(key, value): Upsert key-value pair
- delete(key): Remove key
- list_keys(): All state keys
- load_identity() / load_today() / load_workspace(): Convenience accessors
"""

# Library API: This module provides a programmatic interface to the journal/state/entities
# subsystem for use in Python applications that embed MNEMOS directly.
# The REST API handlers (api/handlers/) use direct asyncpg queries for performance.

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from mnemos.core.auth_context import UserContext

logger = logging.getLogger(__name__)

# Envelope marker that lets get() unambiguously identify a row
# StateManager.set wrote. Lives OUTSIDE the JSON namespace so a
# REST caller payload can never collide with it, no matter the
# shape — including the singleton object {"key": value} which
# the previous in-JSON sentinel approach could not disambiguate.
# A non-empty TEXT prefix that contains a colon and a leading
# letter is guaranteed not to start any valid JSON document, so
# decoders can branch unambiguously.
_SM_ENVELOPE_PREFIX = "MNEMOS_SM:v1:"


def _wrap_state_value(value: Any) -> str:
    """Serialize a Python value into the StateManager envelope.

    On-disk shape is ``MNEMOS_SM:v1:<json>`` (a TEXT prefix
    followed by ``json.dumps(value)``). The prefix is not valid
    JSON, so decoders can branch on it cleanly without risk of
    colliding with caller payloads.
    """
    return _SM_ENVELOPE_PREFIX + json.dumps(value)


def _decode_state_value(raw: Any) -> Any:
    """Reverse the :meth:`StateManager.set` envelope.

    Decode policy (top-down, first match wins):

    1. ``None``/non-string passes through unchanged so an asyncpg
       codec that decodes JSONB→Python on the wire keeps working.
    2. ``MNEMOS_SM:v1:<json>`` prefix returns ``json.loads`` of the
       suffix — canonical StateManager-written row. This is the
       UNAMBIGUOUS path: the prefix can never appear inside a
       valid JSON document, so REST caller payloads of any shape
       (object, array, scalar — including the singleton
       ``{"any_key": value}``) cannot collide.
    3. Invalid JSON returns raw — legacy opaque TEXT written
       before the v4.2.0a5 migration or by external tooling.
    4. JSON object or array (no envelope) returns the decoded
       shape — covers pre-envelope StateManager rows (identity /
       workspace dicts) AND rows written through the REST
       ``/v1/state`` route, which ``json.dumps`` the payload
       without an envelope.
    5. JSON scalar (bare string / number / bool / null) returns
       raw — preserves the distinction between a legacy opaque
       ``"null"`` (string) and a missing row (``None``).

    DOCUMENTED TRADEOFF (case 5): a REST ``/v1/state`` write of a
    bare scalar (``true`` / ``42`` / ``null``) lands in the column
    as ``"true"`` / ``"42"`` / ``"null"``. Because the same on-disk
    shape can come from a legacy opaque TEXT row, the decoder has
    no way to disambiguate, so ``StateManager.get`` returns the
    raw string in both cases. Callers that need typed scalar
    round-tripping through StateManager should write through
    ``StateManager.set`` (which wraps in the envelope). REST
    clients that issue scalar writes already json.loads the raw
    row text returned by GET ``/v1/state/<key>`` and are
    unaffected at their own layer.
    """
    if raw is None or not isinstance(raw, str):
        return raw
    if raw.startswith(_SM_ENVELOPE_PREFIX):
        suffix = raw[len(_SM_ENVELOPE_PREFIX):]
        try:
            return json.loads(suffix)
        except (json.JSONDecodeError, TypeError):
            # Malformed envelope — corrupt row. Surface the raw
            # bytes so callers can at least see what is on disk.
            return raw
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    if isinstance(decoded, (dict, list)):
        return decoded
    # JSON scalar — preserve raw bytes.
    return raw


class StateManager:
    """Manages session state as key-value pairs in the state table."""

    def __init__(self, db_pool=None):
        self.db_pool = db_pool
        self._cache: Dict[tuple[str, str, str], Any] = {}
        # Per-(owner, namespace, key) lock to serialize get/set/delete
        # of the same key. Without this, a slow ``get`` mid-DB-fetch
        # can resume after a fast ``set`` and overwrite the fresh
        # cache entry with the stale row it just read.
        self._key_locks: Dict[tuple[str, str, str], asyncio.Lock] = {}

    def _lock_for(self, cache_key: tuple[str, str, str]) -> asyncio.Lock:
        lock = self._key_locks.get(cache_key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[cache_key] = lock
        return lock

    @staticmethod
    def _scope(user: UserContext) -> tuple[str, str]:
        return user.user_id, user.namespace

    async def get(self, key: str, *, user: UserContext) -> Optional[Any]:
        """Load value for key.

        Symmetry with :meth:`set`: ``set`` writes ``json.dumps(value)``
        into the TEXT column, so ``get`` must json.loads the row back
        to the original Python shape. Without this, a warm-cache
        caller saw a Python dict while a cold-cache caller saw the
        serialized JSON text — a round-trip inconsistency that can
        make load_identity/load_workspace return strings instead of
        the dicts they expect.

        Legacy rows that were written before the v4.2.0a5 TEXT
        migration (or by external tooling) may not be JSON-shaped;
        when json.loads fails, fall back to the raw stored string so
        the call still succeeds.
        """
        owner_id, namespace = self._scope(user)
        cache_key = (owner_id, namespace, key)
        if cache_key in self._cache:
            return self._cache[cache_key]
        if not self.db_pool:
            return None
        async with self._lock_for(cache_key):
            # Re-check the cache under the lock — a concurrent set
            # may have populated it while we were waiting to acquire.
            if cache_key in self._cache:
                return self._cache[cache_key]
            try:
                async with self.db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        'SELECT value FROM state '
                        'WHERE owner_id = $1 AND namespace = $2 AND key = $3 '
                        'AND deleted_at IS NULL',
                        owner_id, namespace, key,
                    )
                    if row:
                        raw = row['value']
                        val = _decode_state_value(raw)
                        self._cache[cache_key] = val
                        return val
            except Exception as e:
                logger.error(f"Error loading state key '{key}': {e}", exc_info=True)
        return None

    async def set(self, key: str, value: Any, *, user: UserContext) -> None:
        """Upsert key-value pair.

        Order of operations is load-bearing: serialize FIRST so a
        non-JSON-serializable value raises before mutating cache;
        durable-write SECOND so a connection failure / lock timeout /
        schema skew also raises before mutating cache; cache update
        LAST so successful return implies the row is on disk. Without
        this ordering, callers can see a "successful" cached value
        while Postgres has the previous value (or no row), and a
        future restart or another worker would see the divergent
        state. Failures propagate (no swallowing) so the caller can
        distinguish a successful write from a silent loss.
        """
        owner_id, namespace = self._scope(user)
        cache_key = (owner_id, namespace, key)
        # 1. Serialize first — surfaces non-JSON values as TypeError
        #    BEFORE any state mutation. The on-disk shape is the
        #    StateManager envelope so reads can distinguish our rows
        #    from legacy opaque TEXT. Cache the JSON-round-tripped
        #    Python shape (not the raw input) so warm-cache reads
        #    return the same shape a cold-cache get would after a
        #    restart: tuples collapse to lists, integer dict keys to
        #    strings, etc.
        serialized = _wrap_state_value(value)
        # Canonical = what _decode_state_value would yield for this
        # serialized envelope. Pre-compute it so the cache update is
        # symmetric with the cold-cache get path.
        # Canonical = what _decode_state_value would yield for
        # this serialized envelope. Strip the prefix and json.loads
        # the JSON suffix.
        canonical = json.loads(serialized[len(_SM_ENVELOPE_PREFIX):])

        async with self._lock_for(cache_key):
            if not self.db_pool:
                # In-memory-only mode: cache IS the persistent store.
                self._cache[cache_key] = canonical
                return

            # 2. Durable write — a failure here MUST not corrupt the
            #    cache. The lock keeps a concurrent ``get`` from
            #    overwriting our about-to-land cache entry with the
            #    pre-write row it just fetched.
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    '''INSERT INTO state (owner_id, namespace, key, value, updated)
                       VALUES ($1, $2, $3, $4, NOW())
                       ON CONFLICT (owner_id, namespace, key) DO UPDATE
                       SET value = $4,
                           updated = NOW(),
                           version = state.version + 1
                       WHERE state.deleted_at IS NULL''',
                    owner_id, namespace, key, serialized,
                )
            # 3. Cache update — only on confirmed durable write. Cache
            #    the canonical (JSON-round-tripped) shape so a warm-
            #    cache get returns exactly what a cold-cache get from
            #    a different worker would, after process restart.
            #    Callers that hit a DB error get the exception
            #    propagated (no silent swallow).
            self._cache[cache_key] = canonical
            logger.debug("Saved state key: %s", key)

    async def delete(self, key: str, *, user: UserContext) -> bool:
        """Delete key. Returns True if it existed."""
        owner_id, namespace = self._scope(user)
        cache_key = (owner_id, namespace, key)
        async with self._lock_for(cache_key):
            self._cache.pop(cache_key, None)
            if not self.db_pool:
                return False
            try:
                async with self.db_pool.acquire() as conn:
                    result = await conn.execute(
                        'DELETE FROM state '
                        'WHERE owner_id = $1 AND namespace = $2 AND key = $3 '
                        'AND deleted_at IS NULL',
                        owner_id, namespace, key,
                    )
                    return result != 'DELETE 0'
            except Exception as e:
                logger.error(f"Error deleting state key '{key}': {e}", exc_info=True)
                return False

    async def list_keys(self, *, user: UserContext) -> List[Dict[str, Any]]:
        """Return all state keys."""
        owner_id, namespace = self._scope(user)
        if not self.db_pool:
            return []
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    'SELECT key, updated FROM state '
                    'WHERE owner_id = $1 AND namespace = $2 '
                    'AND deleted_at IS NULL ORDER BY key',
                    owner_id, namespace,
                )
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error listing state keys: {e}", exc_info=True)
            return []

    # ── Convenience accessors ────────────────────────────────────────────────

    async def load_identity(self, *, user: UserContext) -> Dict[str, Any]:
        val = await self.get('identity', user=user)
        return val or {'id': 'unknown', 'name': 'Unknown User', 'workspace': 'default'}

    async def load_today(self, *, user: UserContext) -> Dict[str, Any]:
        val = await self.get('today', user=user)
        if val:
            return val
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return {
            'date': now.isoformat(),
            'day_of_week': now.strftime('%A'),
            'schedule': [],
            'events': [],
        }

    async def load_workspace(self, *, user: UserContext) -> Dict[str, Any]:
        val = await self.get('workspace', user=user)
        return val or {'id': 'default', 'name': 'Default Workspace', 'projects': []}

    def clear_cache(self) -> None:
        self._cache.clear()
