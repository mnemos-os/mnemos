"""
MNEMOS Test Suite

Comprehensive testing for all modules:
- Unit tests (individual components)
- Integration tests (cross-module interactions)
- E2E tests (full workflows)
"""

# #192: removed orphan `event_loop` fixture defined here. Pytest
# only collects fixtures from `conftest.py`, not from `__init__.py`,
# so this fixture was never exposed to any test and has been dead
# since the v4.0 package restructure. The active loop policy is
# pytest-asyncio's auto-managed loop (mode=Mode.STRICT, configured
# in pyproject.toml).
