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

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from api.auth import UserContext

logger = logging.getLogger(__name__)


class StateManager:
    """Manages session state as key-value pairs in the state table."""

    def __init__(self, db_pool=None):
        self.db_pool = db_pool
        self._cache: Dict[tuple[str, str, str], Any] = {}

    @staticmethod
    def _scope(user: UserContext) -> tuple[str, str]:
        return user.user_id, user.namespace

    async def get(self, key: str, *, user: UserContext) -> Optional[Any]:
        """Load value for key."""
        owner_id, namespace = self._scope(user)
        cache_key = (owner_id, namespace, key)
        if cache_key in self._cache:
            return self._cache[cache_key]
        if not self.db_pool:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT value FROM state '
                    'WHERE owner_id = $1 AND namespace = $2 AND key = $3',
                    owner_id, namespace, key,
                )
                if row:
                    val = row['value']
                    self._cache[cache_key] = val
                    return val
        except Exception as e:
            logger.error(f"Error loading state key '{key}': {e}", exc_info=True)
        return None

    async def set(self, key: str, value: Any, *, user: UserContext) -> None:
        """Upsert key-value pair."""
        owner_id, namespace = self._scope(user)
        self._cache[(owner_id, namespace, key)] = value
        if not self.db_pool:
            return
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    '''INSERT INTO state (owner_id, namespace, key, value, updated)
                       VALUES ($1, $2, $3, $4::jsonb, NOW())
                       ON CONFLICT (owner_id, namespace, key) DO UPDATE
                       SET value = $4::jsonb,
                           updated = NOW(),
                           version = state.version + 1''',
                    owner_id, namespace, key, json.dumps(value),
                )
            logger.debug(f"Saved state key: {key}")
        except Exception as e:
            logger.error(f"Error saving state key '{key}': {e}", exc_info=True)

    async def delete(self, key: str, *, user: UserContext) -> bool:
        """Delete key. Returns True if it existed."""
        owner_id, namespace = self._scope(user)
        self._cache.pop((owner_id, namespace, key), None)
        if not self.db_pool:
            return False
        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.execute(
                    'DELETE FROM state '
                    'WHERE owner_id = $1 AND namespace = $2 AND key = $3',
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
                    'WHERE owner_id = $1 AND namespace = $2 ORDER BY key',
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
