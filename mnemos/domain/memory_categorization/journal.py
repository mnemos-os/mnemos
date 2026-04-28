"""
JournalManager: Date-partitioned journal entry management

Provides:
- append(): Add journal entry
- get_recent(): Get recent entries
- query(): Search journal entries
- get_by_date(): Get entries for specific date
"""

# Library API: This module provides a programmatic interface to the journal/state/entities
# subsystem for use in Python applications that embed MNEMOS directly.
# The REST API handlers (api/handlers/) use direct asyncpg queries for performance.

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from mnemos.api.dependencies import UserContext

logger = logging.getLogger(__name__)


class JournalEntry:
    """Represents a journal entry"""

    def __init__(self, topic: str, content: str, metadata: Optional[Dict] = None):
        self.id = str(uuid4())
        self.topic = topic
        self.content = content
        self.metadata = metadata or {}
        self.created_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'topic': self.topic,
            'content': self.content,
            'metadata': self.metadata,
            'created': self.created_at.isoformat(),
            'entry_date': self.created_at.date().isoformat(),
        }


class JournalManager:
    """Manages journal entries with date partitioning"""

    def __init__(self, db_pool=None):
        self.db_pool = db_pool

    @staticmethod
    def _scope(user: UserContext) -> tuple[str, str]:
        return user.user_id, user.namespace

    async def append(self, topic: str, content: str,
                     metadata: Optional[Dict] = None,
                     *,
                     user: UserContext) -> str:
        entry = JournalEntry(topic, content, metadata)
        logger.debug(f"Adding journal entry: {topic}")
        owner_id, namespace = self._scope(user)

        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        '''INSERT INTO journal
                           (id, owner_id, namespace, entry_date, topic, content, metadata)
                           VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)''',
                        entry.id,
                        owner_id,
                        namespace,
                        entry.created_at.date(),
                        entry.topic,
                        entry.content,
                        json.dumps(entry.metadata),
                    )
                logger.debug(f"Saved journal entry: {entry.id}")
            except Exception as e:
                logger.error(f"Error saving journal entry: {e}", exc_info=True)

        return entry.id

    async def get_recent(
        self,
        count: int = 10,
        topic: Optional[str] = None,
        *,
        user: UserContext,
    ) -> List[Dict]:
        entries = []
        owner_id, namespace = self._scope(user)
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    if topic:
                        rows = await conn.fetch(
                            '''SELECT * FROM journal
                               WHERE owner_id = $1 AND namespace = $2 AND topic = $3
                               ORDER BY created DESC LIMIT $4''',
                            owner_id, namespace, topic, count,
                        )
                    else:
                        rows = await conn.fetch(
                            '''SELECT * FROM journal
                               WHERE owner_id = $1 AND namespace = $2
                               ORDER BY created DESC LIMIT $3''',
                            owner_id, namespace, count,
                        )
                    entries = [dict(row) for row in rows]
            except Exception as e:
                logger.error(f"Error fetching recent entries: {e}", exc_info=True)
        return entries

    async def query(self, search: str, limit: int = 20, *, user: UserContext) -> List[Dict]:
        entries = []
        owner_id, namespace = self._scope(user)
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        '''SELECT * FROM journal
                           WHERE owner_id = $1
                             AND namespace = $2
                             AND (content ILIKE $3 OR topic ILIKE $3)
                           ORDER BY created DESC LIMIT $4''',
                        owner_id, namespace, f'%{search}%', limit,
                    )
                    entries = [dict(row) for row in rows]
            except Exception as e:
                logger.error(f"Error searching journal: {e}", exc_info=True)
        return entries

    async def get_by_date(self, date_str: str, *, user: UserContext) -> List[Dict]:
        entries = []
        owner_id, namespace = self._scope(user)
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        '''SELECT * FROM journal
                           WHERE owner_id = $1 AND namespace = $2 AND entry_date = $3
                           ORDER BY created DESC''',
                        owner_id, namespace, date_str,
                    )
                    entries = [dict(row) for row in rows]
            except Exception as e:
                logger.error(f"Error fetching entries by date: {e}", exc_info=True)
        return entries

    async def get_date_range(
        self,
        start_date: str,
        end_date: str,
        *,
        user: UserContext,
    ) -> List[Dict]:
        entries = []
        owner_id, namespace = self._scope(user)
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        '''SELECT * FROM journal
                           WHERE owner_id = $1
                             AND namespace = $2
                             AND entry_date BETWEEN $3 AND $4
                           ORDER BY created DESC''',
                        owner_id, namespace, start_date, end_date,
                    )
                    entries = [dict(row) for row in rows]
            except Exception as e:
                logger.error(f"Error fetching date range: {e}", exc_info=True)
        return entries

    async def get_statistics(self, *, user: UserContext) -> Dict[str, Any]:
        stats = {
            'total_entries': 0,
            'topics': {},
            'entries_today': 0,
            'entries_this_week': 0,
        }
        owner_id, namespace = self._scope(user)
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    stats['total_entries'] = await conn.fetchval(
                        '''SELECT COUNT(*) FROM journal
                           WHERE owner_id = $1 AND namespace = $2''',
                        owner_id, namespace,
                    ) or 0
                    topic_rows = await conn.fetch(
                        '''SELECT topic, COUNT(*) as count FROM journal
                           WHERE owner_id = $1 AND namespace = $2
                           GROUP BY topic ORDER BY count DESC''',
                        owner_id, namespace,
                    )
                    stats['topics'] = {row['topic']: row['count'] for row in topic_rows}
                    today = datetime.now(timezone.utc).date()
                    stats['entries_today'] = await conn.fetchval(
                        '''SELECT COUNT(*) FROM journal
                           WHERE owner_id = $1 AND namespace = $2 AND entry_date = $3''',
                        owner_id, namespace, today,
                    ) or 0
                    week_ago = datetime.now(timezone.utc).date() - timedelta(days=7)
                    stats['entries_this_week'] = await conn.fetchval(
                        '''SELECT COUNT(*) FROM journal
                           WHERE owner_id = $1 AND namespace = $2 AND entry_date >= $3''',
                        owner_id, namespace, week_ago,
                    ) or 0
            except Exception as e:
                logger.error(f"Error getting statistics: {e}", exc_info=True)
        return stats
