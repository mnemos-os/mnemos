"""
Memory Tier Definitions: 4-tier system with budgets and compression ratios
"""

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class MemoryTier:
    """Represents a memory tier with budget and compression"""

    tier_level: int  # 1-4
    name: str
    token_budget: int
    compression_ratio: float  # Keep X% of tokens
    categories: List[str]
    description: str
    use_case: str

    def to_dict(self) -> Dict:
        return {
            'tier_level': self.tier_level,
            'name': self.name,
            'token_budget': self.token_budget,
            'compression_ratio': self.compression_ratio,
            'categories': self.categories,
            'description': self.description,
            'use_case': self.use_case,
        }


# Tier definitions
TIER_1 = MemoryTier(
    tier_level=1,
    name='Hot - Active Context',
    token_budget=10000,
    compression_ratio=0.20,  # Keep 20% (aggressive compression)
    categories=['facts', 'identity', 'critical_decisions'],
    description='Essential facts only. Highest priority. Aggressively compressed.',
    use_case='Real-time decisions, active problem solving, immediate context needs',
)

TIER_2 = MemoryTier(
    tier_level=2,
    name='Warm - Working Context',
    token_budget=20000,
    compression_ratio=0.35,  # Keep 35% (moderate compression)
    categories=['preferences', 'recent_work', 'active_projects'],
    description='Project-specific context. Moderately compressed.',
    use_case='Multi-session work on same project, detailed requirements, ongoing tasks',
)

TIER_3 = MemoryTier(
    tier_level=3,
    name='Cold - Reference Context',
    token_budget=30000,
    compression_ratio=0.50,  # Keep 50% (light compression)
    categories=['historical_decisions', 'previous_consultations', 'learning'],
    description='Historical context. Lightly compressed.',
    use_case='Learning from past decisions, pattern recognition, optimization decisions',
)

TIER_4 = MemoryTier(
    tier_level=4,
    name='Archive - Complete History',
    token_budget=50000,
    compression_ratio=1.00,  # Keep 100% (no compression)
    categories=['complete_history', 'full_conversations', 'audit_trail'],
    description='Complete uncompressed archive. Never compressed.',
    use_case='Compliance, audit trails, complete reconstruction, long-term reference',
)

# Tier registry
TIERS = {
    1: TIER_1,
    2: TIER_2,
    3: TIER_3,
    4: TIER_4,
}

TIER_NAMES = {
    'hot': TIER_1,
    'warm': TIER_2,
    'cold': TIER_3,
    'archive': TIER_4,
}


def get_tier(tier_level: int) -> MemoryTier:
    """Get tier by level

    Args:
        tier_level: Tier 1-4

    Returns:
        MemoryTier object
    """
    return TIERS.get(tier_level, TIER_2)  # Default to Tier 2


def get_tier_by_name(name: str) -> MemoryTier:
    """Get tier by name

    Args:
        name: 'hot', 'warm', 'cold', or 'archive'

    Returns:
        MemoryTier object
    """
    return TIER_NAMES.get(name.lower(), TIER_2)  # Default to Tier 2


def list_tiers() -> List[MemoryTier]:
    """Get all tiers in order

    Returns:
        List of MemoryTier objects
    """
    return [TIER_1, TIER_2, TIER_3, TIER_4]


def get_tier_compression_budget(tier_level: int) -> int:
    """Get compression budget for tier

    Args:
        tier_level: Tier 1-4

    Returns:
        Token budget
    """
    return get_tier(tier_level).token_budget


def get_tier_compression_ratio(tier_level: int) -> float:
    """Get compression ratio for tier

    Args:
        tier_level: Tier 1-4

    Returns:
        Compression ratio (0.0-1.0, where 1.0 = no compression)
    """
    return get_tier(tier_level).compression_ratio
