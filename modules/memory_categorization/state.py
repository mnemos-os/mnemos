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

import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class StateManager:
    """Manages session state as key-value pairs in the state table."""

    def __init__(self, db_pool=None):
        self.db_pool = db_pool
        self._cache: Dict[str, Any] = {}

    async def get(self, key: str) -> Optional[Any]:
        """Load value for key."""
        if key in self._cache:
            return self._cache[key]
        if not self.db_pool:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow('SELECT value FROM state WHERE key = $1', key)
                if row:
                    val = row['value']
                    self._cache[key] = val
                    return val
        except Exception as e:
            logger.error(f"Error loading state key '{key}': {e}", exc_info=True)
        return None

    async def set(self, key: str, value: Any) -> None:
        """Upsert key-value pair."""
        self._cache[key] = value
        if not self.db_pool:
            return
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    '''INSERT INTO state (key, value, updated)
                       VALUES ($1, $2::jsonb, NOW())
                       ON CONFLICT (key) DO UPDATE SET value = $2::jsonb, updated = NOW()''',
                    key, value
                )
            logger.debug(f"Saved state key: {key}")
        except Exception as e:
            logger.error(f"Error saving state key '{key}': {e}", exc_info=True)

    async def delete(self, key: str) -> bool:
        """Delete key. Returns True if it existed."""
        self._cache.pop(key, None)
        if not self.db_pool:
            return False
        try:
            async with self.db_pool.acquire() as conn:
                result = await conn.execute('DELETE FROM state WHERE key = $1', key)
                return result != 'DELETE 0'
        except Exception as e:
            logger.error(f"Error deleting state key '{key}': {e}", exc_info=True)
            return False

    async def list_keys(self) -> List[Dict[str, Any]]:
        """Return all state keys."""
        if not self.db_pool:
            return []
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch('SELECT key, updated FROM state ORDER BY key')
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error listing state keys: {e}", exc_info=True)
            return []

    # ── Convenience accessors ────────────────────────────────────────────────

    async def load_identity(self) -> Dict[str, Any]:
        val = await self.get('identity')
        return val or {'id': 'unknown', 'name': 'Unknown User', 'workspace': 'default'}

    async def load_today(self) -> Dict[str, Any]:
        val = await self.get('today')
        if val:
            return val
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return {
            'date': now.isoformat(),
            'day_of_week': now.strftime('%A'),
            'schedule': [],
            'events': [],
        }

    async def load_workspace(self) -> Dict[str, Any]:
        val = await self.get('workspace')
        return val or {'id': 'default', 'name': 'Default Workspace', 'projects': []}

    def clear_cache(self) -> None:
        self._cache.clear()
