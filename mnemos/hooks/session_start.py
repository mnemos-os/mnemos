"""
SessionStartHook: Initialize session and load context

Triggered when a new session begins.
Responsibilities:
- Load session metadata
- Initialize state
- Rehydrate context from MNEMOS
- Initialize hooks registry
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4

from mnemos.core.auth_context import UserContext

logger = logging.getLogger(__name__)


class SessionStartHook:
    """Hook for session initialization"""

    def __init__(self, memory_store=None, state_manager=None):
        """Initialize session start hook

        Args:
            memory_store: MemoryStore instance for rehydration
            state_manager: StateManager instance for state operations
        """
        self.memory_store = memory_store
        self.state_manager = state_manager

    async def __call__(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute session start hook

        Args:
            context: Hook context

        Returns:
            Modified context with session info
        """
        logger.info("Session start hook triggered")

        # Generate session ID if not present
        if 'session_id' not in context:
            context['session_id'] = str(uuid4())
            logger.debug(f"Generated session_id: {context['session_id']}")

        # Record session start time
        context['session_start_time'] = datetime.now(timezone.utc).isoformat()

        # Initialize session metadata
        context['session_metadata'] = {
            'session_id': context['session_id'],
            'start_time': context['session_start_time'],
            'hooks_initialized': True,
        }

        # Load state if available
        try:
            if self.state_manager:
                user = context.get('user') or context.get('user_context')
                if user is None:
                    logger.warning(
                        "StateManager configured but no UserContext in hook context; "
                        "skipping state load"
                    )
                    context['state'] = {}
                else:
                    logger.debug("Loading session state from StateManager")
                    state = await self._load_state(user)
                    context['state'] = state
            else:
                logger.debug("No StateManager configured, skipping state load")
        except Exception as e:
            logger.error(f"Error loading session state: {e}", exc_info=True)
            context['state'] = {}

        # Rehydrate context if memory store available
        try:
            if self.memory_store:
                logger.debug("Rehydrating context from MNEMOS")
                rehydrated = await self._rehydrate_context()
                context['rehydrated_memory'] = rehydrated
            else:
                logger.debug("No MemoryStore configured, skipping rehydration")
        except Exception as e:
            logger.error(f"Error rehydrating context: {e}", exc_info=True)
            context['rehydrated_memory'] = {}

        # Initialize session features
        context['features'] = {
            'compression_enabled': True,
            'hooks_enabled': True,
            'quality_tracking': True,
            'audit_logging': True,
        }

        logger.info(f"Session initialized: {context['session_id']}")
        return context

    async def _load_state(self, user: UserContext) -> Dict[str, Any]:
        """Load session state

        Returns:
            State dict with identity, today, workspace
        """
        state = {}

        try:
            # Load identity
            if hasattr(self.state_manager, 'load_identity'):
                state['identity'] = await self.state_manager.load_identity(user=user)
                logger.debug("Loaded identity")
        except Exception as e:
            logger.debug(f"Could not load identity: {e}")

        try:
            # Load today
            if hasattr(self.state_manager, 'load_today'):
                state['today'] = await self.state_manager.load_today(user=user)
                logger.debug("Loaded today")
        except Exception as e:
            logger.debug(f"Could not load today: {e}")

        try:
            # Load workspace
            if hasattr(self.state_manager, 'load_workspace'):
                state['workspace'] = await self.state_manager.load_workspace(user=user)
                logger.debug("Loaded workspace")
        except Exception as e:
            logger.debug(f"Could not load workspace: {e}")

        return state

    async def _rehydrate_context(self) -> Dict[str, Any]:
        """Rehydrate context from memory

        Returns:
            Rehydrated context dict
        """
        rehydrated = {
            'facts': [],
            'identity': [],
            'preferences': [],
            'projects': [],
        }

        try:
            # Query memory store by category
            for category in ['facts', 'identity', 'preferences', 'projects']:
                if hasattr(self.memory_store, 'load_for_rehydration'):
                    memories = await self.memory_store.load_for_rehydration(
                        task_type='reasoning',
                        tier_level=1,  # Use highest priority tier
                        tier_compression_ratio=0.20,
                        limit=20,
                    )
                    if memories:
                        rehydrated[category] = memories
                        logger.debug(f"Loaded {len(memories)} {category} memories")
        except Exception as e:
            logger.error(f"Error rehydrating context: {e}", exc_info=True)

        return rehydrated


async def session_start_hook(context: Dict[str, Any],
                            memory_store=None,
                            state_manager=None,
                            user: UserContext | None = None) -> Dict[str, Any]:
    """Standalone session start hook function

    Args:
        context: Hook context
        memory_store: MemoryStore instance
        state_manager: StateManager instance
        user: Optional UserContext to insert into context for scoped state

    Returns:
        Modified context
    """
    if user is not None:
        context = {**context, 'user': user}
    hook = SessionStartHook(memory_store, state_manager)
    return await hook(context)
