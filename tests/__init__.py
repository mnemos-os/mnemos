"""
MNEMOS Test Suite

Comprehensive testing for all modules:
- Unit tests (individual components)
- Integration tests (cross-module interactions)
- E2E tests (full workflows)
"""

import asyncio

import pytest


# Pytest fixtures
@pytest.fixture
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()
