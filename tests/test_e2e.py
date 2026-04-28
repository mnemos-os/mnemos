"""
End-to-End Tests for MNEMOS API

Tests the importable surface of api_server (Pydantic models, app instance).
"""

import asyncio

import pytest


class TestAPIEndpoints:
    """Test API server models and app instance"""

    def test_api_imports(self):
        """Test API server imports"""
        try:
            from mnemos.api.main import app
            assert app is not None
        except ImportError:
            pytest.skip("API server not available in test environment")

    def test_pydantic_models(self):
        """Test API request/response models"""
        try:
            from mnemos.api.main import HealthResponse, MemoryCreate

            memory = MemoryCreate(
                content="Test memory",
                category="facts",
                task_type="reasoning"
            )
            assert memory.content == "Test memory"
            assert memory.category == "facts"

            health = HealthResponse(
                status="healthy",
                timestamp="2026-02-05T00:00:00Z",
                database_connected=True,
                version="3.2.0"
            )
            assert health.status == "healthy"

        except ImportError:
            pytest.skip("API server models not available")


# Test fixtures
@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
