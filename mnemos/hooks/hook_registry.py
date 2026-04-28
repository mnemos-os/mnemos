"""
HookRegistry: Central event dispatcher for MNEMOS hooks

Manages lifecycle events:
- Session start/end
- Prompt submission
- Memory operations (write, read)
- Rehydration
- Graeae consultation
"""

import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class HookEvent:
    """Represents a hook event"""
    event_type: str
    timestamp: datetime
    context: Dict[str, Any]
    source: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'event_type': self.event_type,
            'timestamp': self.timestamp.isoformat(),
            'context': self.context,
            'source': self.source,
        }


class HookRegistry:
    """Central hook registry and event dispatcher"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize hook registry

        Args:
            config: Configuration dict with enable/disable flags for hooks
        """
        self.config = config or {}
        self.hooks: Dict[str, List[Callable]] = {}
        self.enabled_hooks: set = self._load_enabled_hooks()
        self.history: List[HookEvent] = []
        self.max_history = self.config.get('max_history', 1000)

    def _load_enabled_hooks(self) -> set:
        """Load which hooks are enabled from config"""
        enabled = set()

        if self.config.get('hooks', {}).get('enabled', True):
            if self.config.get('hooks', {}).get('session_start', True):
                enabled.add('session.start')
            if self.config.get('hooks', {}).get('prompt_submit', True):
                enabled.add('prompt.submit')
            if self.config.get('hooks', {}).get('memory_write', True):
                enabled.add('memory.write')
            if self.config.get('hooks', {}).get('memory_read', False):
                enabled.add('memory.read')
            if self.config.get('hooks', {}).get('rehydration_start', True):
                enabled.add('rehydration.start')
            if self.config.get('hooks', {}).get('graeae_consult', True):
                enabled.add('graeae.consult')

        return enabled

    def register(self, event_type: str, callback: Callable) -> None:
        """Register a hook callback for an event

        Args:
            event_type: Event name (e.g., 'session.start')
            callback: Async callable to invoke
        """
        if event_type not in self.hooks:
            self.hooks[event_type] = []

        self.hooks[event_type].append(callback)
        # Auto-enable event type when globally enabled
        if self.config.get('hooks', {}).get('enabled', True):
            self.enabled_hooks.add(event_type)
        logger.debug(f"Registered hook for {event_type}: {getattr(callback, '__name__', repr(callback))}")

    def unregister(self, event_type: str, callback: Callable) -> None:
        """Unregister a hook callback

        Args:
            event_type: Event name
            callback: Callable to remove
        """
        if event_type in self.hooks and callback in self.hooks[event_type]:
            self.hooks[event_type].remove(callback)
            logger.debug(f"Unregistered hook for {event_type}: {getattr(callback, '__name__', repr(callback))}")

    def list_hooks(self, event_type: Optional[str] = None) -> Dict[str, List[str]]:
        """List all registered hooks

        Args:
            event_type: Filter by event type (None = all)

        Returns:
            Dict mapping event types to list of callback names
        """
        if event_type:
            return {
                event_type: [
                    cb.__name__ for cb in self.hooks.get(event_type, [])
                ]
            }

        return {
            event: [getattr(cb, '__name__', repr(cb)) for cb in callbacks]
            for event, callbacks in self.hooks.items()
        }

    async def trigger(self, event_type: str, context: Dict[str, Any],
                     source: Optional[str] = None) -> Dict[str, Any]:
        """Trigger hooks for an event

        Args:
            event_type: Event name
            context: Context dict to pass to hooks
            source: Optional source identifier

        Returns:
            Modified context after all hooks have run (or original if hook disabled)
        """
        # Check if hook is enabled
        if event_type not in self.enabled_hooks:
            logger.debug(f"Hook {event_type} is disabled")
            return context

        # Create event
        event = HookEvent(
            event_type=event_type,
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            context=context.copy(),
            source=source,
        )

        # Record in history
        self._record_event(event)

        # Get hooks for this event
        callbacks = self.hooks.get(event_type, [])
        if not callbacks:
            logger.debug(f"No hooks registered for {event_type}")
            return context

        logger.debug(f"Triggering {len(callbacks)} hooks for {event_type}")

        # Run each hook
        modified_context = context.copy()
        for callback in callbacks:
            try:
                # Call hook (async if needed)
                if inspect.iscoroutinefunction(callback):
                    result = await callback(modified_context)
                else:
                    result = callback(modified_context)

                # Merge result back into context
                if isinstance(result, dict):
                    modified_context.update(result)

                logger.debug(f"Hook {getattr(callback, '__name__', repr(callback))} completed successfully")

            except Exception as e:
                # Log error but don't crash
                logger.error(f"Error in hook {getattr(callback, '__name__', repr(callback))} for {event_type}: {e}",
                           exc_info=True)
                # Continue with next hook

        return modified_context

    def _record_event(self, event: HookEvent) -> None:
        """Record event in history

        Args:
            event: HookEvent to record
        """
        self.history.append(event)

        # Trim history if too large
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]

    def get_history(self, event_type: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Get hook execution history

        Args:
            event_type: Filter by event type (None = all)
            limit: Maximum number of events to return

        Returns:
            List of event dicts
        """
        if event_type:
            events = [e for e in self.history if e.event_type == event_type]
        else:
            events = self.history

        # Return most recent first
        return [e.to_dict() for e in events[-limit:]][::-1]

    def clear_history(self) -> None:
        """Clear hook execution history"""
        self.history.clear()
        logger.debug("Cleared hook history")

    def enable_hook(self, event_type: str) -> None:
        """Enable a hook type

        Args:
            event_type: Hook event type to enable
        """
        self.enabled_hooks.add(event_type)
        logger.info(f"Enabled hook: {event_type}")

    def disable_hook(self, event_type: str) -> None:
        """Disable a hook type

        Args:
            event_type: Hook event type to disable
        """
        self.enabled_hooks.discard(event_type)
        logger.info(f"Disabled hook: {event_type}")

    def is_enabled(self, event_type: str) -> bool:
        """Check if a hook is enabled

        Args:
            event_type: Hook event type

        Returns:
            True if enabled
        """
        return event_type in self.enabled_hooks
