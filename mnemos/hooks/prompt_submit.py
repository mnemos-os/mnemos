"""
PromptSubmitHook: Process prompt submission

Triggered when a prompt is submitted by the user.
Responsibilities:
- Pre-process prompt
- Detect task type
- Select memory tier
- Prepare context for LLM
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


class PromptSubmitHook:
    """Hook for prompt submission processing"""

    # Task detection keywords
    TASK_KEYWORDS = {
        'architecture_design': ['architecture', 'design', 'microservices', 'system', 'flow'],
        'code_generation': ['code', 'implement', 'write', 'function', 'class', 'method'],
        'debugging': ['bug', 'error', 'debug', 'issue', 'fix', 'problem'],
        'refactoring': ['refactor', 'cleanup', 'optimize', 'improve', 'restructure'],
        'api_design': ['api', 'endpoint', 'rest', 'graphql', 'schema'],
        'data_modeling': ['database', 'schema', 'model', 'entity', 'relationship'],
        'reasoning': ['why', 'how', 'explain', 'analyze', 'think', 'reason', 'architecture'],
        'documentation': ['document', 'readme', 'guide', 'tutorial', 'example'],
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize prompt submit hook

        Args:
            config: Configuration dict
        """
        self.config = config or {}

    async def __call__(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute prompt submit hook

        Args:
            context: Hook context with 'prompt' key

        Returns:
            Modified context with task metadata
        """
        prompt = context.get('prompt', '')
        if not prompt:
            logger.warning("No prompt in context")
            return context

        logger.info(f"Prompt submit hook triggered (length: {len(prompt)})")

        # Generate request ID
        request_id = str(uuid4())
        context['request_id'] = request_id

        # Record submission time
        context['prompt_submitted_at'] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

        # Detect task type
        task_type = self._detect_task_type(prompt)
        context['detected_task_type'] = task_type
        logger.debug(f"Detected task type: {task_type}")

        # Select memory tier based on prompt complexity
        tier_level = self._select_tier_level(prompt)
        context['selected_tier_level'] = tier_level
        logger.debug(f"Selected memory tier: {tier_level}")

        # Get tier compression ratio
        tier_ratios = {1: 0.20, 2: 0.35, 3: 0.50, 4: 1.00}
        context['tier_compression_ratio'] = tier_ratios.get(tier_level, 0.35)

        # Prepare memory loading hints
        context['load_memory'] = {
            'task_type': task_type,
            'tier_level': tier_level,
            'compression_ratio': context['tier_compression_ratio'],
            'limit': self._get_memory_limit(tier_level),
        }

        # Estimate token count
        token_count = self._estimate_tokens(prompt)
        context['prompt_tokens'] = token_count
        logger.debug(f"Estimated prompt tokens: {token_count}")

        # Prepare metadata
        context['prompt_metadata'] = {
            'request_id': request_id,
            'submitted_at': context['prompt_submitted_at'],
            'task_type': task_type,
            'tier_level': tier_level,
            'prompt_tokens': token_count,
            'length': len(prompt),
        }

        logger.info(f"Prompt processed: {task_type} (tier {tier_level}, {token_count} tokens)")
        return context

    def _detect_task_type(self, prompt: str) -> str:
        """Detect task type from prompt keywords

        Args:
            prompt: User prompt

        Returns:
            Task type string
        """
        prompt_lower = prompt.lower()
        scores = {}

        # Score each task type based on keyword matches
        for task_type, keywords in self.TASK_KEYWORDS.items():
            score = sum(1 for keyword in keywords if keyword in prompt_lower)
            if score > 0:
                scores[task_type] = score

        # Return task type with highest score, default to reasoning
        if scores:
            return max(scores, key=scores.get)
        return 'reasoning'

    def _select_tier_level(self, prompt: str) -> int:
        """Select memory tier based on prompt complexity

        Args:
            prompt: User prompt

        Returns:
            Tier level 1-4
        """
        # Simple heuristics: length and question count
        length = len(prompt)
        question_count = prompt.count('?')
        exclamation_count = prompt.count('!')

        complexity_score = (length / 100) + question_count + exclamation_count

        if complexity_score > 50:
            return 1  # Aggressive compression for complex tasks
        elif complexity_score > 30:
            return 2  # Moderate compression
        elif complexity_score > 10:
            return 3  # Light compression
        else:
            return 4  # No compression for simple tasks

    def _get_memory_limit(self, tier_level: int) -> int:
        """Get number of memories to load for tier

        Args:
            tier_level: Memory tier 1-4

        Returns:
            Number of memories to load
        """
        limits = {
            1: 5,    # Aggressive: load less
            2: 10,   # Moderate: load more
            3: 15,   # Light: load more
            4: 20,   # Archive: load all
        }
        return limits.get(tier_level, 10)

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count for text

        Args:
            text: Text to estimate

        Returns:
            Approximate token count
        """
        # Simple heuristic: 1 token per 4 characters
        return (len(text) + 3) // 4

    @staticmethod
    def get_task_keywords() -> Dict[str, List[str]]:
        """Get task detection keywords

        Returns:
            Dict of task types to keywords
        """
        return PromptSubmitHook.TASK_KEYWORDS


async def prompt_submit_hook(context: Dict[str, Any],
                            config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Standalone prompt submit hook function

    Args:
        context: Hook context
        config: Configuration dict

    Returns:
        Modified context
    """
    hook = PromptSubmitHook(config)
    return await hook(context)
