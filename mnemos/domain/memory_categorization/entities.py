"""
EntityManager: Track entities and their relationships.

DB schema uses related_entities UUID[] array (no join table).

Provides:
- create_entity(): Create entity (person, project, concept, etc.)
- get_entity(): Fetch single entity
- link_entities(): Add entity ID to related_entities array
- query_entities(): Search entities by type or name
- get_related_entities(): Traverse entity relationships
- delete_entity(): Remove entity
"""

# Library API: This module provides a programmatic interface to the journal/state/entities
# subsystem for use in Python applications that embed MNEMOS directly.
# The REST API handlers (api/handlers/) use direct asyncpg queries for performance.

import json
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4

from mnemos.core.auth_context import UserContext
from mnemos.domain.memory_categorization.constants import ENTITY_TYPES

logger = logging.getLogger(__name__)


class EntityManager:
    """Manages entities using the entities table (related_entities UUID[] for links)."""

    def __init__(self, db_pool=None):
        self.db_pool = db_pool

    @staticmethod
    def _scope(user: UserContext) -> tuple[str, str]:
        return user.user_id, user.namespace

    async def create_entity(self, entity_type: str, name: str,
                            description: Optional[str] = None,
                            metadata: Optional[Dict] = None,
                            *,
                            user: UserContext) -> Optional[str]:
        """Create entity. Returns entity id or None on error."""
        if entity_type not in ENTITY_TYPES:
            logger.warning(f"Unknown entity type: {entity_type}")
        entity_id = str(uuid4())
        owner_id, namespace = self._scope(user)
        if not self.db_pool:
            return entity_id
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    '''INSERT INTO entities
                       (id, owner_id, namespace, entity_type, name, description, metadata)
                       VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                       ON CONFLICT (owner_id, namespace, entity_type, name) DO UPDATE
                       SET description = COALESCE($6, entities.description),
                           updated = NOW()
                       WHERE entities.deleted_at IS NULL
                       RETURNING id::text''',
                    entity_id, owner_id, namespace, entity_type, name,
                    description, json.dumps(metadata or {}),
                )
            logger.debug(f"Created entity: {entity_type}/{name}")
            return row["id"] if row else None
        except Exception as e:
            logger.error(f"Error creating entity: {e}", exc_info=True)
            return None

    async def get_entity(self, entity_id: str, *, user: UserContext) -> Optional[Dict]:
        """Fetch entity by id."""
        owner_id, namespace = self._scope(user)
        if not self.db_pool:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    '''SELECT * FROM entities
                       WHERE id = $1::uuid AND owner_id = $2 AND namespace = $3
                         AND deleted_at IS NULL''',
                    entity_id, owner_id, namespace,
                )
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error fetching entity: {e}", exc_info=True)
            return None

    async def get_by_name(
        self,
        entity_type: str,
        name: str,
        *,
        user: UserContext,
    ) -> Optional[Dict]:
        """Fetch entity by type+name."""
        owner_id, namespace = self._scope(user)
        if not self.db_pool:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    '''SELECT * FROM entities
                       WHERE owner_id = $1
                         AND namespace = $2
                         AND entity_type = $3
                         AND name = $4
                         AND deleted_at IS NULL''',
                    owner_id, namespace, entity_type, name,
                )
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error fetching entity by name: {e}", exc_info=True)
            return None

    async def link_entities(
        self,
        entity_id: str,
        related_id: str,
        *,
        user: UserContext,
    ) -> bool:
        """Add related_id to entity's related_entities array (bidirectional)."""
        owner_id, namespace = self._scope(user)
        if not self.db_pool:
            return False
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    '''UPDATE entities
                       SET related_entities = array_append(
                           COALESCE(related_entities, ARRAY[]::uuid[]),
                           $2::uuid
                       ),
                       updated = NOW()
                       WHERE id = $1::uuid
                         AND owner_id = $3
                         AND namespace = $4
                         AND deleted_at IS NULL
                         AND NOT ($2::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[])))''',
                    entity_id, related_id, owner_id, namespace,
                )
                # Also link in reverse
                await conn.execute(
                    '''UPDATE entities
                       SET related_entities = array_append(
                           COALESCE(related_entities, ARRAY[]::uuid[]),
                           $2::uuid
                       ),
                       updated = NOW()
                       WHERE id = $1::uuid
                         AND owner_id = $3
                         AND namespace = $4
                         AND deleted_at IS NULL
                         AND NOT ($2::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[])))''',
                    related_id, entity_id, owner_id, namespace,
                )
            logger.debug(f"Linked entities: {entity_id} <-> {related_id}")
            return True
        except Exception as e:
            logger.error(f"Error linking entities: {e}", exc_info=True)
            return False

    async def query_entities(self, entity_type: Optional[str] = None,
                             name_search: Optional[str] = None,
                             limit: int = 50,
                             *,
                             user: UserContext) -> List[Dict]:
        """Search entities."""
        owner_id, namespace = self._scope(user)
        if not self.db_pool:
            return []
        try:
            async with self.db_pool.acquire() as conn:
                if entity_type and name_search:
                    rows = await conn.fetch(
                        '''SELECT * FROM entities
                           WHERE owner_id = $1
                             AND namespace = $2
                             AND entity_type = $3
                         AND name ILIKE $4
                         AND deleted_at IS NULL
                           ORDER BY name LIMIT $5''',
                        owner_id, namespace, entity_type, f'%{name_search}%', limit,
                    )
                elif entity_type:
                    rows = await conn.fetch(
                        '''SELECT * FROM entities
                           WHERE owner_id = $1 AND namespace = $2 AND entity_type = $3
                             AND deleted_at IS NULL
                           ORDER BY name LIMIT $4''',
                        owner_id, namespace, entity_type, limit,
                    )
                elif name_search:
                    rows = await conn.fetch(
                        '''SELECT * FROM entities
                           WHERE owner_id = $1
                             AND namespace = $2
                             AND name ILIKE $3
                             AND deleted_at IS NULL
                           ORDER BY name LIMIT $4''',
                        owner_id, namespace, f'%{name_search}%', limit,
                    )
                else:
                    rows = await conn.fetch(
                        '''SELECT * FROM entities
                           WHERE owner_id = $1 AND namespace = $2
                             AND deleted_at IS NULL
                           ORDER BY entity_type, name LIMIT $3''',
                        owner_id, namespace, limit,
                    )
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error querying entities: {e}", exc_info=True)
            return []

    async def get_related_entities(
        self,
        entity_id: str,
        *,
        user: UserContext,
    ) -> List[Dict]:
        """Get all entities linked to this one via related_entities array."""
        entity = await self.get_entity(entity_id, user=user)
        if not entity or not entity.get('related_entities'):
            return []
        related_ids = entity['related_entities']
        owner_id, namespace = self._scope(user)
        if not related_ids or not self.db_pool:
            return []
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    '''SELECT * FROM entities
                       WHERE owner_id = $1
                         AND namespace = $2
                         AND id = ANY($3::uuid[])
                         AND deleted_at IS NULL''',
                    owner_id, namespace, related_ids,
                )
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error fetching related entities: {e}", exc_info=True)
            return []

    async def update_entity(self, entity_id: str,
                            description: Optional[str] = None,
                            metadata: Optional[Dict] = None,
                            *,
                            user: UserContext) -> bool:
        """Update entity description/metadata."""
        owner_id, namespace = self._scope(user)
        if not self.db_pool:
            return False
        try:
            async with self.db_pool.acquire() as conn:
                if description is not None and metadata is not None:
                    await conn.execute(
                        '''UPDATE entities
                           SET description = $1, metadata = $2::jsonb, updated = NOW()
                           WHERE id = $3::uuid AND owner_id = $4 AND namespace = $5
                             AND deleted_at IS NULL''',
                        description, json.dumps(metadata), entity_id, owner_id, namespace,
                    )
                elif description is not None:
                    await conn.execute(
                        '''UPDATE entities
                           SET description = $1, updated = NOW()
                           WHERE id = $2::uuid AND owner_id = $3 AND namespace = $4
                             AND deleted_at IS NULL''',
                        description, entity_id, owner_id, namespace,
                    )
                elif metadata is not None:
                    await conn.execute(
                        '''UPDATE entities
                           SET metadata = $1::jsonb, updated = NOW()
                           WHERE id = $2::uuid AND owner_id = $3 AND namespace = $4
                             AND deleted_at IS NULL''',
                        json.dumps(metadata), entity_id, owner_id, namespace,
                    )
            return True
        except Exception as e:
            logger.error(f"Error updating entity: {e}", exc_info=True)
            return False

    async def delete_entity(self, entity_id: str, *, user: UserContext) -> bool:
        """Delete entity and remove from other entities' related_entities arrays."""
        owner_id, namespace = self._scope(user)
        if not self.db_pool:
            return False
        try:
            async with self.db_pool.acquire() as conn:
                # Remove from other entities' arrays first
                await conn.execute(
                    '''UPDATE entities
                       SET related_entities = array_remove(related_entities, $1::uuid)
                       WHERE owner_id = $2
                         AND namespace = $3
                         AND deleted_at IS NULL
                         AND $1::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[]))''',
                    entity_id, owner_id, namespace,
                )
                result = await conn.execute(
                    '''DELETE FROM entities
                       WHERE id = $1::uuid AND owner_id = $2 AND namespace = $3
                         AND deleted_at IS NULL''',
                    entity_id, owner_id, namespace,
                )
                return result != 'DELETE 0'
        except Exception as e:
            logger.error(f"Error deleting entity: {e}", exc_info=True)
            return False

    async def get_statistics(self, *, user: UserContext) -> Dict[str, Any]:
        stats = {'total_entities': 0, 'by_type': {}}
        owner_id, namespace = self._scope(user)
        if not self.db_pool:
            return stats
        try:
            async with self.db_pool.acquire() as conn:
                stats['total_entities'] = await conn.fetchval(
                    '''SELECT COUNT(*) FROM entities
                       WHERE owner_id = $1 AND namespace = $2
                         AND deleted_at IS NULL''',
                    owner_id, namespace,
                ) or 0
                type_rows = await conn.fetch(
                    '''SELECT entity_type, COUNT(*) as count FROM entities
                       WHERE owner_id = $1 AND namespace = $2
                         AND deleted_at IS NULL
                       GROUP BY entity_type ORDER BY count DESC''',
                    owner_id, namespace,
                )
                stats['by_type'] = {row['entity_type']: row['count'] for row in type_rows}
        except Exception as e:
            logger.error(f"Error getting entity stats: {e}", exc_info=True)
        return stats
