"""Integration tests for MNEMOS v3.0.0 — critical paths only.

Tests verify:
1. OpenAI gateway structure (no actual API calls)
2. Session management schema
3. DAG commit hash generation
4. Memory injection pipeline
5. Model optimizer integration
6. Distillation worker registration
"""

import sys
from pathlib import Path

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestOpenAIGatewayStructure:
    """Verify gateway components exist and are wired correctly."""

    def test_openai_compat_handler_exists(self):
        """Gateway handler module imports without error."""
        try:
            from mnemos.api.routes import openai_compat
            assert hasattr(openai_compat, 'router')
            assert openai_compat.router.routes  # Has registered routes
        except ImportError as e:
            pytest.fail(f"openai_compat handler missing: {e}")

    def test_openai_compat_router_registered(self):
        """OpenAI router is registered in app."""
        try:
            import mnemos.api.main as api_server
            route_paths = {route.path for route in api_server.app.routes}
            # Should have /v1/chat/completions and /v1/models endpoints
            v1_routes = {r for r in route_paths if '/v1/' in r}
            assert len(v1_routes) > 0, "No /v1/* routes found"
        except Exception as e:
            pytest.fail(f"Router registration check failed: {e}")

    def test_gateway_imports_graeae(self):
        """Gateway can import GRAEAE routing."""
        try:
            from mnemos.api.routes.openai_compat import _route_to_provider
            assert callable(_route_to_provider)
        except ImportError as e:
            pytest.fail(f"GRAEAE routing not available: {e}")

    def test_gateway_imports_mnemos_search(self):
        """Gateway can import MNEMOS search."""
        try:
            from mnemos.api.routes.openai_compat import _search_mnemos_context
            assert callable(_search_mnemos_context)
        except ImportError as e:
            pytest.fail(f"MNEMOS search not available: {e}")


class TestSessionManagementStructure:
    """Verify session handler exists and is properly wired."""

    def test_sessions_handler_exists(self):
        """Session handler module imports without error."""
        try:
            from mnemos.api.routes import sessions
            assert hasattr(sessions, 'router')
            assert sessions.router.routes
        except ImportError as e:
            pytest.fail(f"sessions handler missing: {e}")

    def test_sessions_router_registered(self):
        """Sessions router is registered in app."""
        try:
            import mnemos.api.main as api_server
            route_paths = {route.path for route in api_server.app.routes}
            session_routes = {r for r in route_paths if '/sessions' in r}
            assert len(session_routes) > 0, "No /sessions routes found"
        except Exception as e:
            pytest.fail(f"Sessions router registration check failed: {e}")

    def test_session_models_exist(self):
        """Session models are defined and importable."""
        try:
            from mnemos.domain.models import (
                SessionContext,
                SessionHistoryResponse,
                SessionMessage,
                SessionRequest,
                SessionResponse,
            )
            # Verify BaseModel inheritance
            assert hasattr(SessionContext, 'model_fields')
            assert hasattr(SessionRequest, 'model_fields')
            assert hasattr(SessionResponse, 'model_fields')
            assert hasattr(SessionMessage, 'model_fields')
            assert hasattr(SessionHistoryResponse, 'model_fields')
        except ImportError as e:
            pytest.fail(f"Session models missing: {e}")


class TestDAGImplementation:
    """Verify DAG (git-like versioning) is properly implemented."""

    def test_dag_handler_exists(self):
        """DAG handler module imports without error."""
        try:
            from mnemos.api.routes import dag
            assert hasattr(dag, 'router')
            assert dag.router.routes
        except ImportError as e:
            pytest.fail(f"dag handler missing: {e}")

    def test_dag_router_registered(self):
        """DAG router is registered in app."""
        try:
            import mnemos.api.main as api_server
            route_paths = {route.path for route in api_server.app.routes}
            dag_routes = {r for r in route_paths if '/branches' in r or '/commits' in r}
            assert len(dag_routes) > 0, "No DAG routes found"
        except Exception as e:
            pytest.fail(f"DAG router registration check failed: {e}")

    def test_dag_models_exist(self):
        """DAG models are defined."""
        try:
            from mnemos.api.routes.dag import BranchCreateRequest, BranchInfo, CommitInfo
            assert hasattr(CommitInfo, 'model_fields')
            assert hasattr(BranchInfo, 'model_fields')
            assert hasattr(BranchCreateRequest, 'model_fields')
        except ImportError as e:
            pytest.fail(f"DAG models missing: {e}")

    def test_migration_v3_dag_exists(self):
        """DAG migration file exists and is readable."""
        migration_path = Path(__file__).parent.parent / 'db' / 'migrations_v3_dag.sql'
        assert migration_path.exists(), f"DAG migration not found at {migration_path}"
        content = migration_path.read_text()
        # Verify key DAG concepts are in migration
        assert 'commit_hash' in content
        assert 'parent_version_id' in content
        assert 'memory_branches' in content


class TestCompressionStackV33:
    """Verify the v3.3 going-forward stack (APOLLO + ARTEMIS).

    Retired engine modules and CompressionManager stay removed. See
    EVOLUTION.md "v3.2 tail" for the rationale."""

    def test_apollo_engine_importable(self):
        """APOLLO is the schema-aware engine in the v3.3 stack."""
        try:
            from mnemos.domain.compression.apollo import APOLLOEngine
            assert APOLLOEngine.id == "apollo"
        except ImportError as e:
            pytest.fail(f"APOLLOEngine missing: {e}")

    def test_artemis_engine_importable(self):
        """ARTEMIS is the CPU-only extractive engine in the v3.3 stack."""
        try:
            from mnemos.domain.compression.artemis import ARTEMISEngine
            assert ARTEMISEngine.id == "artemis"
        except ImportError as e:
            pytest.fail(f"ARTEMISEngine missing: {e}")

    def test_retired_engines_removed(self):
        """LETHE / ANAMNESIS / ALETHEIA must NOT be importable anymore.
        If a future commit re-introduces them, this test catches it."""
        for module_name in ("mnemos.domain.compression.lethe", "mnemos.domain.compression.anamnesis", "mnemos.domain.compression.aletheia"):
            try:
                __import__(module_name)
                pytest.fail(f"{module_name} should be gone in v3.3 — found it importable")
            except ImportError:
                pass  # expected

    def test_compression_manager_removed(self):
        """CompressionManager was deleted in v3.3."""
        try:
            __import__("mnemos.domain.compression.manager")
            pytest.fail("mnemos.domain.compression.manager should be gone in v3.3")
        except ImportError:
            pass  # expected


class TestDistillationWorkerIntegration:
    """Verify worker is integrated into lifecycle."""

    def test_worker_in_lifecycle(self):
        """Distillation worker is registered in lifecycle."""
        try:
            from mnemos.core import lifecycle
            assert hasattr(lifecycle, '_run_distillation_worker')
            assert hasattr(lifecycle, '_worker_status')
        except ImportError as e:
            pytest.fail(f"Worker lifecycle integration missing: {e}")

    def test_worker_status_in_health(self):
        """Worker status is included in health check."""
        try:
            from mnemos.domain.models import HealthResponse
            # HealthResponse should have distillation_worker field
            fields = HealthResponse.model_fields
            assert 'distillation_worker' in fields
        except Exception as e:
            pytest.fail(f"Worker status in health check failed: {e}")


class TestModelOptimizerIntegration:
    """Verify optimizer is wired into gateway."""

    def test_model_registry_recommend_exists(self):
        """Model recommendation endpoint exists."""
        try:
            from mnemos.api.routes.providers import recommend_model
            assert callable(recommend_model)
        except ImportError as e:
            pytest.fail(f"Model recommendation endpoint missing: {e}")

    def test_optimizer_integration(self):
        """Gateway calls optimizer for auto model selection."""
        try:
            from mnemos.api.routes.openai_compat import _get_model_recommendation
            assert callable(_get_model_recommendation)
        except ImportError as e:
            pytest.fail(f"Optimizer integration missing: {e}")


class TestMCPToolsIntegration:
    """Verify MCP tools are available for programmatic DAG/optimizer access."""

    def test_mcp_tools_module_exists(self):
        """MCP tools module exists."""
        try:
            from mnemos.mcp import tools as mcp_tools
            assert hasattr(mcp_tools, 'TOOLS')
        except ImportError as e:
            pytest.fail(f"MCP tools module missing: {e}")

    def test_required_tools_exist(self):
        """All required MCP tools are registered."""
        try:
            from mnemos.mcp.tools import TOOLS
            required_tools = {
                'log_memory',
                'branch_memory',
                'diff_memory_commits',
                'checkout_memory',
                'recommend_model',
            }
            actual_tools = set(TOOLS.keys())
            assert required_tools.issubset(actual_tools), \
                f"Missing tools: {required_tools - actual_tools}"
        except Exception as e:
            pytest.fail(f"MCP tools check failed: {e}")


class TestDockerIntegration:
    """Verify Docker-related files and configurations."""

    def test_dockerfile_uses_uv(self):
        """Dockerfile uses uv for faster builds."""
        dockerfile_path = Path(__file__).parent.parent / 'Dockerfile'
        assert dockerfile_path.exists(), "Dockerfile not found"
        content = dockerfile_path.read_text()
        assert 'uv pip install' in content, "Dockerfile doesn't use uv"

    def test_gpu_setup_script_exists(self):
        """GPU detection helper script exists."""
        script_path = Path(__file__).parent.parent / 'docker-gpu-setup.sh'
        assert script_path.exists(), "docker-gpu-setup.sh not found"
        content = script_path.read_text()
        # Verify it detects multiple GPU types
        assert 'nvidia-smi' in content
        assert 'rocm-smi' in content
        assert 'Metal' in content or 'metal' in content.lower()

    def test_pyproject_toml_version_updated(self):
        """pyproject.toml reflects current version."""
        pyproject_path = Path(__file__).parent.parent / 'pyproject.toml'
        assert pyproject_path.exists(), "pyproject.toml not found"
        content = pyproject_path.read_text()
        assert 'version = "4.1.2"' in content, "Version not at 4.1.2"

    def test_pyproject_toml_has_uv_config(self):
        """pyproject.toml has uv configuration."""
        pyproject_path = Path(__file__).parent.parent / 'pyproject.toml'
        content = pyproject_path.read_text()
        assert '[tool.uv]' in content, "uv configuration missing"


class TestAntiMemoryPoisoning:
    """Verify anti-memory poisoning documentation exists."""

    def test_guide_exists(self):
        """Anti-memory poisoning guide is present."""
        guide_path = Path(__file__).parent.parent / 'ANTI_MEMORY_POISONING.md'
        assert guide_path.exists(), "ANTI_MEMORY_POISONING.md not found"

    def test_guide_covers_key_concepts(self):
        """Guide explains DAG protection mechanisms."""
        guide_path = Path(__file__).parent.parent / 'ANTI_MEMORY_POISONING.md'
        content = guide_path.read_text()
        key_concepts = {
            'immutable',
            'audit trail',
            'parent pointer',
            'commit_hash',
            'branch isolation',
        }
        for concept in key_concepts:
            assert concept.lower() in content.lower(), \
                f"Guide doesn't mention '{concept}'"


class TestV3Surface:
    """Verify the v3 public surface is present."""

    def test_expected_endpoints_present(self):
        """Core v3 routes are mounted on the app."""
        try:
            import mnemos.api.main as api_server
            route_paths = {route.path for route in api_server.app.routes}
            expected_routes = {'/v1/memories', '/v1/consultations', '/health'}
            for route in expected_routes:
                matching = {r for r in route_paths if route in r}
                assert len(matching) > 0, f"Expected route pattern {route} not found"
        except Exception as e:
            pytest.fail(f"v3 surface check failed: {e}")

    def test_memory_model_fields(self):
        """MemoryItem exposes the core v3 fields."""
        try:
            from mnemos.domain.models import MemoryItem
            required_fields = {'id', 'content', 'category', 'created', 'metadata'}
            actual_fields = set(MemoryItem.model_fields.keys())
            assert required_fields.issubset(actual_fields), \
                f"MemoryItem missing expected fields: {required_fields - actual_fields}"
        except Exception as e:
            pytest.fail(f"MemoryItem field check failed: {e}")


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
