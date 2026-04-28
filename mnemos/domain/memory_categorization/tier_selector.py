"""
TierSelector: Map tasks to memory tiers

Selects appropriate memory tier based on:
- Task type
- Complexity
- History
- Context availability
"""

import logging
from typing import Dict, List, Optional

from .tiers import TIERS, MemoryTier

logger = logging.getLogger(__name__)


class TierSelector:
    """Selects memory tier for task"""

    def __init__(self, config: Optional[Dict] = None):
        """Initialize tier selector

        Args:
            config: Configuration dict with task_detection keywords
        """
        self.config = config or {}
        self.task_keywords = self._load_task_keywords()

    def _load_task_keywords(self) -> Dict[str, List[str]]:
        """Load task detection keywords from config

        Returns:
            Dict of task type to keywords
        """
        task_detection = self.config.get('memory', {}).get('task_detection', {})
        return {
            'infrastructure': task_detection.get('infrastructure', [
                'deploy', 'devops', 'infrastructure', 'kubernetes', 'docker', 'cloud'
            ]),
            'reasoning': task_detection.get('reasoning', [
                'architecture', 'design', 'decision', 'plan', 'strategy', 'analysis'
            ]),
            'code': task_detection.get('code', [
                'code', 'implementation', 'debug', 'refactor', 'algorithm'
            ]),
            'project': task_detection.get('project', [
                'project', 'planning', 'timeline', 'milestone', 'task'
            ]),
            'complex': task_detection.get('complex', [
                'complex', 'sophisticated', 'integration', 'system'
            ]),
        }

    def select_tiers(self, task_type: str, complexity: str = 'medium') -> List[MemoryTier]:
        """Select appropriate memory tiers for task

        Args:
            task_type: Type of task (code, reasoning, etc.)
            complexity: 'simple', 'medium', 'complex'

        Returns:
            List of MemoryTier objects in priority order
        """
        logger.debug(f"Selecting tiers for {task_type} (complexity: {complexity})")

        # Simple tasks: just Tier 2
        if complexity == 'simple':
            return [TIERS[2]]

        # Medium tasks: Tier 2 + 3
        if complexity == 'medium':
            return [TIERS[2], TIERS[3]]

        # Complex tasks: All tiers
        if complexity == 'complex':
            return [TIERS[1], TIERS[2], TIERS[3], TIERS[4]]

        # Default to medium
        return [TIERS[2], TIERS[3]]

    def select_single_tier(self, task_type: str, complexity: str = 'medium') -> MemoryTier:
        """Select single primary memory tier for task

        Args:
            task_type: Type of task
            complexity: Complexity level

        Returns:
            Primary MemoryTier
        """
        tiers = self.select_tiers(task_type, complexity)
        return tiers[0] if tiers else TIERS[2]

    def select_by_token_budget(self, needed_tokens: int) -> MemoryTier:
        """Select tier based on token budget needed

        Args:
            needed_tokens: Number of tokens needed

        Returns:
            MemoryTier with sufficient budget
        """
        for tier_level in [1, 2, 3, 4]:
            tier = TIERS[tier_level]
            if tier.token_budget >= needed_tokens:
                logger.debug(f"Selected tier {tier_level} for {needed_tokens} tokens")
                return tier

        # Default to highest tier
        return TIERS[4]

    def detect_complexity(self, task_description: str) -> str:
        """Detect task complexity from description

        Args:
            task_description: Text describing the task

        Returns:
            'simple', 'medium', or 'complex'
        """
        description_lower = task_description.lower()

        # Count complexity indicators
        complex_count = sum(1 for word in self.task_keywords.get('complex', [])
                          if word in description_lower)
        simple_count = len(description_lower.split())

        if complex_count > 3 or simple_count > 200:
            return 'complex'
        elif simple_count > 50:
            return 'medium'
        else:
            return 'simple'

    def recommend_tiers(self, prompt: str) -> Dict:
        """Get comprehensive tier recommendation for prompt

        Args:
            prompt: User prompt/task description

        Returns:
            Dict with recommendations
        """
        complexity = self.detect_complexity(prompt)
        tiers = self.select_tiers('general', complexity)
        primary_tier = tiers[0]

        return {
            'complexity': complexity,
            'primary_tier': primary_tier.to_dict(),
            'available_tiers': [t.to_dict() for t in tiers],
            'total_token_budget': sum(t.token_budget for t in tiers),
            'compression_ratio': primary_tier.compression_ratio,
        }
