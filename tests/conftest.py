"""Pytest configuration and shared fixtures for MNEMOS tests."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clear_slowapi_limiter(limiter: Any) -> None:
    reset = getattr(limiter, "reset", None)
    if callable(reset):
        reset()
        return

    storage = getattr(limiter, "_storage", None)
    for method_name in ("reset", "clear"):
        method = getattr(storage, method_name, None)
        if callable(method):
            method()
            return

    for attr_name in ("storage", "_cache"):
        cache = getattr(storage, attr_name, None)
        clear = getattr(cache, "clear", None)
        if callable(clear):
            clear()
            return


@pytest.fixture(autouse=True)
def reset_rate_limiter_state():
    """Prevent SlowAPI's in-memory buckets from leaking across tests."""
    from api.rate_limit import limiter

    _clear_slowapi_limiter(limiter)


def _build_memory_row(
    memory_id: str,
    content: str,
    category: str = "solutions",
    subcategory: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _utcnow()
    return {
        "id": memory_id,
        "content": content,
        "category": category,
        "subcategory": subcategory,
        "created": now,
        "updated": now,
        "metadata": metadata or {"source": "test"},
        "quality_rating": 75,
        "compressed_content": None,
        "verbatim_content": content,
        "owner_id": "default",
        "group_id": None,
        "namespace": "default",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
    }


class _AsyncNullContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


# ── FakePool coverage note ────────────────────────────────────────────────────
# FakeConnection dispatches on substring matches against the SQL fragments our
# handlers issue today. If a handler's SQL drifts (column renames, alias
# changes, new WHERE clauses) this dispatch silently misses — returning None
# or the wrong canned response — and the regression only surfaces against
# real postgres. That's what produced the `created AS created_at` issue in
# v3.0.0. When you change a handler query, add a matching branch below. For
# deeper safety, run the suite against a real postgres container in CI.
class FakeConnection:
    def __init__(self, state: dict[str, Any]):
        self.state = state

    def transaction(self):
        return _AsyncNullContext()

    async def execute(self, query: str, *args):
        compact = " ".join(query.split())
        if compact.startswith("SELECT pg_advisory_xact_lock") or compact == "SELECT 1":
            return "SELECT 1"

        if compact.startswith("INSERT INTO memories "):
            memory_id = args[0]
            self.state["memories"][memory_id] = _build_memory_row(
                memory_id=memory_id,
                content=args[1],
                category=args[2],
                subcategory=args[3],
                metadata=args[4],
            )
            return "INSERT 0 1"

        if compact.startswith("INSERT INTO memory_versions "):
            return "INSERT 0 1"

        if compact.startswith("INSERT INTO consultation_memory_refs "):
            consultation_id, memory_id = args[:2]
            existing = {
                (ref["consultation_id"], ref["memory_id"]) for ref in self.state["memory_refs"]
            }
            if (consultation_id, memory_id) not in existing:
                self.state["memory_refs"].append(
                    {
                        "consultation_id": consultation_id,
                        "memory_id": memory_id,
                        "relevance_score": None,
                        "injected_at": _utcnow(),
                    }
                )
            return "INSERT 0 1"

        if compact.startswith("INSERT INTO graeae_audit_log "):
            (
                consultation_id,
                prompt,
                prompt_hash,
                provider,
                response_text,
                response_hash,
                chain_hash,
                prev_id,
                prev_chain_hash,
                task_type,
                quality_score,
            ) = args
            sequence_num = len(self.state["audit_log"]) + 1
            self.state["audit_log"].append(
                {
                    "id": f"audit-{sequence_num}",
                    "sequence_num": sequence_num,
                    "consultation_id": consultation_id,
                    "prompt": prompt,
                    "prompt_hash": prompt_hash,
                    "provider": provider,
                    "response_text": response_text,
                    "response_hash": response_hash,
                    "chain_hash": chain_hash,
                    "prev_id": prev_id,
                    "prev_chain_hash": prev_chain_hash,
                    "task_type": task_type,
                    "quality_score": quality_score,
                    "created_at": _utcnow(),
                }
            )
            return "INSERT 0 1"

        if compact.startswith("DELETE FROM memories WHERE id = $1 AND owner_id = $2"):
            memory_id = args[0]
            memory = self.state["memories"].get(memory_id)
            if memory and memory.get("owner_id") in (args[1], None):
                self.state["memories"].pop(memory_id, None)
                return "DELETE 1"
            return "DELETE 0"

        if compact.startswith("DELETE FROM memories WHERE id = $1"):
            memory_id = args[0]
            if self.state["memories"].pop(memory_id, None):
                return "DELETE 1"
            return "DELETE 0"

        return "OK"

    async def fetchrow(self, query: str, *args):
        compact = " ".join(query.split())

        if compact.startswith("INSERT INTO graeae_consultations"):
            consultation_id = f"consult-{len(self.state['consultations']) + 1}"
            record = {
                "id": consultation_id,
                "prompt": args[0],
                "task_type": args[1],
                "consensus_response": args[2],
                "consensus_score": args[3],
                "winning_muse": args[4],
                "cost": args[5],
                "latency_ms": args[6],
                "mode": args[7],
                "owner_id": args[8],
                "created": _utcnow(),
            }
            self.state["consultations"][consultation_id] = record
            return {"id": consultation_id}

        if "SELECT id, prompt, task_type, consensus_response" in compact:
            consultation = self.state["consultations"].get(args[0])
            if "AND owner_id = $2" in compact and (
                not consultation or consultation.get("owner_id") != args[1]
            ):
                return None
            return consultation

        if "SELECT id, created FROM graeae_consultations WHERE id = $1" in compact:
            consultation = self.state["consultations"].get(args[0])
            if "AND owner_id = $2" in compact and (
                not consultation or consultation.get("owner_id") != args[1]
            ):
                return None
            if not consultation:
                return None
            return {"id": consultation["id"], "created": consultation["created"]}

        if "SELECT id, chain_hash FROM graeae_audit_log" in compact:
            return self.state["audit_log"][-1] if self.state["audit_log"] else None

        # owner-scoped ownership probe from memory PATCH — checked FIRST
        # because the unscoped string below is a substring of this one.
        if "SELECT id FROM memories WHERE id=$1 AND owner_id=$2" in compact:
            memory = self.state["memories"].get(args[0])
            if memory and memory.get("owner_id") in (args[1], None):
                return {"id": memory["id"]}
            return None

        if "SELECT id FROM memories WHERE id=$1" in compact:
            memory = self.state["memories"].get(args[0])
            return {"id": memory["id"]} if memory else None

        # owner + namespace check used by DAG _assert_memory_access
        # (v3.1.2 Tier 3 follow-up extended this to two-dimensional tenancy)
        if compact.startswith("SELECT owner_id, namespace FROM memories WHERE id ="):
            memory = self.state["memories"].get(args[0])
            if not memory:
                return None
            return {
                "owner_id": memory.get("owner_id"),
                "namespace": memory.get("namespace"),
            }

        # Legacy single-column path kept for any handler still on it
        if compact.startswith("SELECT owner_id FROM memories WHERE id ="):
            memory = self.state["memories"].get(args[0])
            return {"owner_id": memory.get("owner_id")} if memory else None

        # branch existence check used by create_branch
        if compact.startswith("SELECT id FROM memory_branches WHERE memory_id = $1 AND name = $2"):
            return None  # tests don't pre-populate branches

        if "SELECT cmr.consultation_id, cmr.memory_id, cmr.relevance_score FROM consultation_memory_refs" in compact:
            return self.state["memory_refs"][0] if self.state["memory_refs"] else None

        if "FROM memories WHERE id=$1" in compact:
            return self.state["memories"].get(args[0])

        return None

    async def fetch(self, query: str, *args):
        compact = " ".join(query.split())

        if "FROM graeae_audit_log" in compact and "ORDER BY" in compact:
            rows = list(self.state["audit_log"])
            if "expected_prev_hash" in compact:
                prev_hash_by_id = {}
                prev_chain_hash = None
                for row in sorted(rows, key=lambda item: item["sequence_num"]):
                    prev_hash_by_id[row["id"]] = prev_chain_hash
                    prev_chain_hash = row["chain_hash"]
                rows = [
                    {
                        **row,
                        "expected_prev_hash": prev_hash_by_id[row["id"]],
                    }
                    for row in rows
                ]
            if "scoped_sequence_num" in compact:
                owner_id = args[0]
                visible_rows = [
                    row for row in rows
                    if (
                        self.state["consultations"]
                        .get(row["consultation_id"], {})
                        .get("owner_id") == owner_id
                    )
                ]
                rows = []
                scoped_prev_id = None
                verifier_query = "expected_prev_hash" in compact
                for scoped_sequence_num, row in enumerate(
                    sorted(visible_rows, key=lambda item: item["sequence_num"]),
                    start=1,
                ):
                    if verifier_query:
                        rows.append(
                            {
                                **row,
                                "scoped_sequence_num": scoped_sequence_num,
                            }
                        )
                    else:
                        rows.append(
                            {
                                **row,
                                "global_sequence_num": row["sequence_num"],
                                "sequence_num": scoped_sequence_num,
                                "prev_id": scoped_prev_id,
                            }
                        )
                    scoped_prev_id = row["id"]
            elif "c.owner_id = $1" in compact:
                owner_id = args[0]
                rows = [
                    row for row in rows
                    if (
                        self.state["consultations"]
                        .get(row["consultation_id"], {})
                        .get("owner_id") == owner_id
                    )
                ]
            if (
                "ORDER BY global_sequence_num DESC" in compact
                or "FROM graeae_audit_log ORDER BY sequence_num DESC LIMIT" in compact
                or "ORDER BY al.sequence_num DESC" in compact
            ):
                rows = list(reversed(rows))
            return rows

        if "FROM consultation_memory_refs WHERE consultation_id = $1" in compact:
            return [
                ref for ref in self.state["memory_refs"] if ref["consultation_id"] == args[0]
            ]

        if "SELECT indexname FROM pg_indexes" in compact:
            return [
                {"indexname": "idx_consultation_memory_refs_consultation"},
                {"indexname": "idx_consultation_memory_refs_memory"},
                {"indexname": "idx_consultation_memory_refs_injected_at"},
            ]

        if "SELECT column_name FROM information_schema.columns WHERE table_name='graeae_audit_log'" in compact:
            return [
                {"column_name": "id"},
                {"column_name": "prompt"},
                {"column_name": "prompt_hash"},
                {"column_name": "response_hash"},
                {"column_name": "chain_hash"},
            ]

        if "FROM model_registry" in compact:
            return list(self.state["model_registry"])

        if "FROM memories" in compact:
            memories = list(self.state["memories"].values())
            if "to_tsvector" in compact or "content ILIKE" in compact:
                needle = str(args[0]).strip("%").lower()
                memories = [m for m in memories if needle in m["content"].lower()]
            if "category=$1" in compact or "category=$3" in compact:
                category = next((arg for arg in args if arg in {"solutions", "system_tests"}), None)
                if category is not None:
                    memories = [m for m in memories if m["category"] == category]
            return memories

        return []

    async def fetchval(self, query: str, *args):
        compact = " ".join(query.split())

        if "WHERE table_name='consultation_memory_refs'" in compact:
            return True
        if "WHERE table_name='graeae_audit_log'" in compact:
            return True
        if compact == "SELECT COUNT(*) FROM memories":
            return len(self.state["memories"])
        if "SELECT EXISTS(SELECT 1 FROM graeae_consultations WHERE id=$1)" in compact:
            return args[0] in self.state["consultations"]
        if "SELECT EXISTS(SELECT 1 FROM memories WHERE id=$1)" in compact:
            return args[0] in self.state["memories"]
        return None


class _AcquireContext:
    def __init__(self, conn: FakeConnection):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self):
        seed_memory = _build_memory_row(
            memory_id="mem_seed_001",
            content="Test memory content for integration search",
        )
        self.state = {
            "memories": {seed_memory["id"]: seed_memory},
            "consultations": {},
            "audit_log": [],
            "memory_refs": [],
            "model_registry": [
                {
                    "provider": "openai",
                    "model_id": "gpt-4o-mini",
                    "display_name": "GPT-4o mini",
                    "input_cost_per_mtok": 0.15,
                    "output_cost_per_mtok": 0.6,
                    "capabilities": ["reasoning", "logic"],
                    "graeae_weight": 0.92,
                    "context_window": 128000,
                }
            ],
        }
        self._conn = FakeConnection(self.state)

    def acquire(self):
        return _AcquireContext(self._conn)


@pytest.fixture
def db_pool():
    """Deterministic fake asyncpg pool for in-process API tests."""
    return FakePool()


@pytest.fixture
def auth_headers():
    """Return authorization headers for authenticated routes when enabled."""
    return {"Authorization": "Bearer test-token-for-testing"}


@pytest.fixture
def mock_graeae_engine():
    """Mock GRAEAE engine for testing."""
    engine = MagicMock()
    engine.consult = AsyncMock(
        return_value={
            "all_responses": {
                "openai": {
                    "response_text": "Paris is the capital of France.",
                    "final_score": 0.95,
                    "latency_ms": 1200,
                    "status": "success",
                    "model_id": "gpt-4o-mini",
                }
            },
            "consensus_response": "Paris is the capital of France.",
            "consensus_score": 0.95,
            "winning_muse": "openai",
            "cost": 0.02,
            "latency_ms": 1200,
            "timestamp": _utcnow().isoformat(),
            "memory_ids": ["mem_seed_001"],
        }
    )
    engine.provider_status = MagicMock(
        return_value={
            "providers": {
                "openai": {"status": "healthy"},
                "anthropic": {"status": "healthy"},
            },
            "cache": {"entries": 0, "hits": 0, "misses": 0, "hit_rate": 0.0},
        }
    )
    engine.providers = {
        "openai": {"model": "gpt-4o-mini"},
        "anthropic": {"model": "claude-3-5-sonnet"},
    }
    return engine


@pytest_asyncio.fixture
async def client(monkeypatch, db_pool: FakePool, mock_graeae_engine):
    """Create an in-process AsyncClient bound to the FastAPI app."""
    import api.lifecycle as lc
    from api.auth import configure_auth
    from api_server import app
    import graeae.engine as graeae_engine

    configure_auth({"enabled": False})
    monkeypatch.setattr(lc, "_pool", db_pool)
    monkeypatch.setattr(lc, "_cache", None)
    monkeypatch.setattr(lc, "_worker_status", {"distillation_worker": "idle"})
    monkeypatch.setattr(graeae_engine, "get_graeae_engine", lambda: mock_graeae_engine)
    app.state.pool = db_pool

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def sample_consultation_id(client: AsyncClient, auth_headers: dict):
    """Create a sample consultation and return its ID for testing."""
    resp = await client.post(
        "/v1/consultations",
        json={"prompt": "Sample consultation for testing", "task_type": "reasoning"},
        headers=auth_headers,
    )
    if resp.status_code == 200:
        data = resp.json()
        return data.get("consultation_id") or data.get("id") or "test-id"
    return "test-id"
