"""MPF /v1/export and /v1/import contract tests (v3.2).

Direct-handler tests with a mocked asyncpg connection. Verifies:

  * Export is scoped to the caller's owner_id + namespace for non-root.
  * Root can target any owner/namespace via query params.
  * Non-root passing cross-owner/ns query params -> 403 (explicit
    rejection, not silent narrowing).
  * Envelope shape matches MPF v0.1: mpf_version, records[] with
    kind='memory', payload_version='mnemos-3.1'.
  * Import stamps the caller's owner_id + namespace on every record
    by default (non-root can't smuggle other owners' rows in).
  * Root with preserve_owner=true honors envelope's owner/namespace.
  * Non-root with preserve_owner=true -> 403.
  * Unknown record kinds counted under unsupported_kinds and skipped.
  * Payload-version mismatch counted as skipped with an error.
  * Empty content counted as failed.
  * Envelope mpf_version mismatch -> 415.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from api.auth import UserContext
from api.handlers import portability


def _alice() -> UserContext:
    return UserContext(
        user_id="alice", group_ids=[], role="user",
        namespace="alice-ns", authenticated=True,
    )


def _root() -> UserContext:
    return UserContext(
        user_id="admin", group_ids=[], role="root",
        namespace="default", authenticated=True,
    )


class _Conn:
    def __init__(self, rows=None, *, routed_rows=None):
        """Mock asyncpg connection.

        ``rows`` is the default row set for any fetch.
        ``routed_rows`` is an optional dict mapping a substring (e.g.
        the table name "kg_triples") to the rows that should be
        returned when the SQL contains that substring. Used by the
        sidecar tests to seed different row sets per query without
        building a full SQL parser.
        """
        self._rows = rows or []
        self._routed = routed_rows or {}
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.executes: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        for needle, payload in self._routed.items():
            if needle in sql:
                return payload
        return self._rows

    async def execute(self, sql: str, *args):
        self.executes.append((sql, args))
        # Default: INSERT successful. Tests can override the conn to
        # simulate ON CONFLICT DO NOTHING (INSERT 0 0) or failures.
        return "INSERT 0 1"

    def transaction(self):
        class _NullCtx:
            async def __aenter__(self_): return self_
            async def __aexit__(self_, *a): return False
        return _NullCtx()


class _PoolCtx:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


def _install(monkeypatch, conn):
    import api.lifecycle as lc
    pool = MagicMock()
    pool.acquire = lambda: _PoolCtx(conn)
    monkeypatch.setattr(lc, "_pool", pool)


def _memory_row(
    id: str = "mem_alice1",
    owner_id: str = "alice",
    namespace: str = "alice-ns",
    category: str = "solutions",
    content: str = "hello",
):
    return {
        "id": id, "content": content, "category": category, "subcategory": None,
        "created": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "owner_id": owner_id, "namespace": namespace, "permission_mode": 600,
        "quality_rating": 75,
        "source_model": None, "source_provider": None,
        "source_session": None, "source_agent": None,
        "metadata": {"imported_from": "test"},
    }


# ─── /v1/export ──────────────────────────────────────────────────────────────


def test_export_filters_by_caller_owner_and_namespace_for_non_root(monkeypatch):
    conn = _Conn(rows=[_memory_row()])
    _install(monkeypatch, conn)

    asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=None, namespace=None, include_sidecars=False, user=_alice(),
    ))

    sql, args = conn.fetch_calls[-1]
    assert "owner_id = $" in sql
    assert "namespace = $" in sql
    assert "alice" in args
    assert "alice-ns" in args


def test_export_non_root_cross_owner_param_rejected(monkeypatch):
    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portability.export_memories(
            category=None, limit=1000, offset=0,
            owner_id="bob", namespace=None, include_sidecars=False, user=_alice(),
        ))
    assert exc.value.status_code == 403
    # No DB fetch should have happened
    assert not conn.fetch_calls


def test_export_non_root_cross_namespace_param_rejected(monkeypatch):
    conn = _Conn(rows=[])
    _install(monkeypatch, conn)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portability.export_memories(
            category=None, limit=1000, offset=0,
            owner_id=None, namespace="other-ns", include_sidecars=False, user=_alice(),
        ))
    assert exc.value.status_code == 403


def test_export_root_may_target_arbitrary_slice(monkeypatch):
    conn = _Conn(rows=[_memory_row(owner_id="bob", namespace="bob-ns")])
    _install(monkeypatch, conn)

    result = asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id="bob", namespace="bob-ns", include_sidecars=False, user=_root(),
    ))
    sql, args = conn.fetch_calls[-1]
    assert "bob" in args
    assert "bob-ns" in args
    assert result.record_count == 1


def test_export_envelope_shape(monkeypatch):
    row = _memory_row()
    conn = _Conn(rows=[row])
    _install(monkeypatch, conn)

    env = asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=None, namespace=None, include_sidecars=False, user=_alice(),
    ))

    assert env.mpf_version == "0.1.1"
    assert env.source_system == "mnemos"
    assert len(env.records) == 1
    rec = env.records[0]
    assert rec.kind == "memory"
    assert rec.payload_version == "mnemos-3.1"
    assert rec.id == "mem_alice1"
    assert rec.payload["content"] == "hello"
    # Timestamps ISO-serialized
    assert "2026-01" in rec.payload["created"]


def test_export_strips_none_payload_fields(monkeypatch):
    row = _memory_row()
    row["source_model"] = None
    row["source_provider"] = None
    conn = _Conn(rows=[row])
    _install(monkeypatch, conn)

    env = asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=None, namespace=None, include_sidecars=False, user=_alice(),
    ))
    payload = env.records[0].payload
    assert "source_model" not in payload
    assert "source_provider" not in payload


# ─── /v1/export — sidecar emission (CHARON v0.2) ────────────────────────────


def _kg_row(id: str = "kg_1", memory_id: str = "mem_alice1"):
    return {
        "id": id,
        "subject": "Paris",
        "predicate": "capitalOf",
        "object": "France",
        "subject_type": "place",
        "object_type": "place",
        "valid_from": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "valid_until": None,
        "memory_id": memory_id,
        "confidence": 0.95,
        "created": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "owner_id": "alice",
        "namespace": "alice-ns",
    }


def _mv_row(id: str = "ver_1", memory_id: str = "mem_alice1"):
    return {
        "id": id,
        "memory_id": memory_id,
        "version_num": 1,
        "content": "hello",
        "category": "solutions",
        "subcategory": None,
        "metadata": {"src": "test"},
        "verbatim_content": "hello verbatim",
        "owner_id": "alice",
        "namespace": "alice-ns",
        "permission_mode": 600,
        "source_model": None,
        "source_provider": None,
        "source_session": None,
        "source_agent": None,
        "snapshot_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "snapshot_by": "alice",
        "change_type": "create",
        "commit_hash": "abc123",
        "parent_version_id": None,
        "branch": "main",
        "merge_parents": None,
    }


def _cv_row(memory_id: str = "mem_alice1"):
    return {
        "memory_id": memory_id,
        "owner_id": "alice",
        "winner_candidate_id": None,
        "engine_id": "apollo",
        "engine_version": "1.0",
        "compressed_content": "compressed:hello",
        "compressed_tokens": 4,
        "compression_ratio": 2.5,
        "quality_score": 0.87,
        "composite_score": 0.81,
        "scoring_profile": "balanced",
        "judge_model": "claude-opus-4-7",
        "selected_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def test_export_omits_sidecars_by_default(monkeypatch):
    """include_sidecars=False (the default) must not even SELECT from
    sidecar tables — old envelopes stay byte-identical."""
    conn = _Conn(rows=[_memory_row()])
    _install(monkeypatch, conn)

    env = asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=None, namespace=None, include_sidecars=False, user=_alice(),
    ))

    sql_seen = " | ".join(s for s, _ in conn.fetch_calls)
    assert "kg_triples" not in sql_seen
    assert "memory_versions" not in sql_seen
    assert "memory_compressed_variants" not in sql_seen
    assert env.kg_triples is None
    assert env.memory_versions is None
    assert env.compression_manifest is None


def test_export_with_sidecars_emits_three_arrays(monkeypatch):
    """include_sidecars=True emits all three sidecar arrays, scoped
    to the same owner+namespace as the memories query."""
    conn = _Conn(
        rows=[_memory_row()],
        routed_rows={
            "FROM kg_triples": [_kg_row()],
            "FROM memory_versions": [_mv_row()],
            "FROM memory_compressed_variants": [_cv_row()],
        },
    )
    _install(monkeypatch, conn)

    env = asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=None, namespace=None, include_sidecars=True, user=_alice(),
    ))

    assert env.kg_triples is not None and len(env.kg_triples) == 1
    assert env.memory_versions is not None and len(env.memory_versions) == 1
    assert env.compression_manifest is not None and len(env.compression_manifest) == 1

    # KG triple shape
    kg = env.kg_triples[0]
    assert kg["predicate"] == "capitalOf"
    assert kg["subject_literal"] == "Paris"
    assert kg["object_literal"] == "France"
    assert kg["subject_type"] == "place"
    assert kg["confidence"] == 0.95

    # memory_version shape carries DAG + snapshot fields
    mv = env.memory_versions[0]
    assert mv["record_id"] == "mem_alice1"
    assert mv["commit_hash"] == "abc123"
    assert mv["branch"] == "main"
    assert mv["change_type"] == "create"
    assert mv["verbatim_content"] == "hello verbatim"

    # compression_manifest shape carries judge + ratio fields
    cm = env.compression_manifest[0]
    assert cm["record_id"] == "mem_alice1"
    assert cm["engine_id"] == "apollo"
    assert cm["quality_score"] == 0.87
    assert cm["compressed_tokens"] == 4


def test_export_sidecars_constrained_to_exported_memory_ids(monkeypatch):
    """memory_versions / memory_compressed_variants queries must
    restrict to the exported memory id set so a category-filtered
    export doesn't drag in all-time DAG history."""
    rows: list[tuple[str, tuple]] = []

    class _CapturingConn(_Conn):
        async def fetch(self, sql, *args):
            rows.append((sql, args))
            return await super().fetch(sql, *args)

    conn = _CapturingConn(
        rows=[_memory_row(id="mem_42")],
        routed_rows={
            "FROM kg_triples": [],
            "FROM memory_versions": [],
            "FROM memory_compressed_variants": [],
        },
    )
    _install(monkeypatch, conn)

    asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=None, namespace=None, include_sidecars=True, user=_alice(),
    ))

    mv_sql, mv_args = next((s, a) for s, a in rows if "FROM memory_versions" in s)
    cv_sql, cv_args = next((s, a) for s, a in rows if "FROM memory_compressed_variants" in s)
    kg_sql, _ = next((s, a) for s, a in rows if "FROM kg_triples" in s)

    # memory_versions + compression_manifest must include the
    # memory_id IN (...) clause and the exported id alice's
    # mem_42 must appear in the params.
    assert "memory_id = ANY" in mv_sql
    assert ["mem_42"] in (list(mv_args) or [])
    assert "memory_id = ANY" in cv_sql
    # KG triples are not bound to memory_ids — must NOT have ANY clause
    assert "memory_id = ANY" not in kg_sql


def test_export_empty_memory_set_yields_empty_sidecars(monkeypatch):
    """Empty memory result skips bound-to-memories sidecar queries
    entirely (no DB hit) and returns empty arrays."""
    conn = _Conn(rows=[])  # no memories
    _install(monkeypatch, conn)

    env = asyncio.run(portability.export_memories(
        category=None, limit=1000, offset=0,
        owner_id=None, namespace=None, include_sidecars=True, user=_alice(),
    ))

    assert env.records == []
    # kg_triples is still queried (not bound to memories), so empty list ≠ None
    assert env.kg_triples == []
    assert env.memory_versions == []
    assert env.compression_manifest == []

    # Verify no SELECT FROM memory_versions or memory_compressed_variants
    sql_seen = " | ".join(s for s, _ in conn.fetch_calls)
    assert "FROM memory_versions" not in sql_seen
    assert "FROM memory_compressed_variants" not in sql_seen


# ─── /v1/import ──────────────────────────────────────────────────────────────


def _envelope(records):
    return portability.MPFEnvelope(
        mpf_version="0.1.0",
        source_system="mnemos",
        records=records,
    )


def _memory_record(
    id: str = "mem_1",
    content: str = "body",
    owner_id: str = "alice",
    namespace: str = "alice-ns",
    category: str = "solutions",
    payload_version: str = "mnemos-3.1",
):
    return portability.MPFRecord(
        id=id,
        kind="memory",
        payload_version=payload_version,
        payload={
            "content": content,
            "category": category,
            "owner_id": owner_id,
            "namespace": namespace,
        },
    )


def test_import_forces_caller_owner_for_non_root(monkeypatch):
    """Non-root imports rewrite owner_id + namespace on every record
    so a malicious envelope can't smuggle bob's rows into alice's
    account by labeling them with bob's id."""
    conn = _Conn()
    _install(monkeypatch, conn)

    env = _envelope([_memory_record(owner_id="bob", namespace="bob-ns")])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 1
    # The INSERT args should bind alice's identity, not bob's
    insert = next(e for e in conn.executes if "INSERT INTO memories" in e[0])
    args = insert[1]
    assert "alice" in args
    assert "alice-ns" in args
    assert "bob" not in args


def test_import_root_with_preserve_owner_honors_envelope(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = _envelope([_memory_record(owner_id="bob", namespace="bob-ns")])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=True, user=_root(),
    ))
    assert stats.imported == 1
    insert = next(e for e in conn.executes if "INSERT INTO memories" in e[0])
    args = insert[1]
    assert "bob" in args
    assert "bob-ns" in args


def test_import_non_root_preserve_owner_rejected(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = _envelope([_memory_record()])

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portability.import_memories(
            envelope=env, preserve_owner=True, user=_alice(),
        ))
    assert exc.value.status_code == 403


def test_import_counts_unsupported_kinds(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = portability.MPFEnvelope(records=[
        portability.MPFRecord(id="doc_1", kind="document", payload_version="1.10.0", payload={}),
        portability.MPFRecord(id="fact_1", kind="fact", payload_version="mpf-0.1", payload={}),
        _memory_record(),
    ])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 1
    assert stats.unsupported_kinds == {"document": 1, "fact": 1}


def test_import_payload_version_mismatch_skipped(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = _envelope([_memory_record(payload_version="mnemos-2.4")])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 0
    assert stats.skipped == 1
    assert any("mnemos-2.4" in e for e in stats.errors)


def test_import_empty_content_fails(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = _envelope([_memory_record(content="  ")])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 0
    assert stats.failed == 1
    assert any("empty content" in e for e in stats.errors)


def test_import_wrong_mpf_version_returns_415(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)
    env = portability.MPFEnvelope(mpf_version="999.0.0", records=[])

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portability.import_memories(
            envelope=env, preserve_owner=False, user=_alice(),
        ))
    assert exc.value.status_code == 415


def test_import_idempotent_on_id_collision(monkeypatch):
    """ON CONFLICT DO NOTHING surfaces as INSERT 0 0, which the handler
    counts as skipped (not imported). Re-importing the same envelope
    should not double-count."""
    class _DupeConn(_Conn):
        async def execute(self, sql, *args):
            self.executes.append((sql, args))
            return "INSERT 0 0"  # always conflict
    conn = _DupeConn()
    _install(monkeypatch, conn)

    env = _envelope([_memory_record(id="mem_dupe")])
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 0
    assert stats.skipped == 1


def test_import_accepts_011_envelope(monkeypatch):
    """Forward-compat ratchet: 0.1.1 envelopes import cleanly against
    the same handler. The required-fields contract didn't change in
    the patch bump."""
    conn = _Conn()
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        mpf_version="0.1.1",
        records=[_memory_record(id="mem_011")],
    )
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 1


# ─── /v1/import — sidecar consumption (CHARON v0.2) ─────────────────────────


def _kg_sidecar_entry(
    id: str = "kg_1",
    predicate: str = "capitalOf",
    subject: str = "Paris",
    obj: str = "France",
    owner_id: str = "alice",
    namespace: str = "alice-ns",
):
    return {
        "id": id,
        "predicate": predicate,
        "subject_literal": subject,
        "object_literal": obj,
        "subject_type": "place",
        "object_type": "place",
        "memory_id": "mem_alice1",
        "confidence": 0.9,
        "valid_from": "2026-01-01T00:00:00+00:00",
        "created": "2026-01-01T00:00:00+00:00",
        "owner_id": owner_id,
        "namespace": namespace,
    }


def _mv_sidecar_entry(
    id: str = "00000000-0000-0000-0000-000000000001",
    record_id: str = "mem_alice1",
    owner_id: str = "alice",
    namespace: str = "alice-ns",
):
    return {
        "id": id,
        "record_id": record_id,
        "version_num": 1,
        "content": "version body",
        "category": "solutions",
        "verbatim_content": "verbatim",
        "owner_id": owner_id,
        "namespace": namespace,
        "permission_mode": 600,
        "snapshot_at": "2026-01-01T00:00:00+00:00",
        "change_type": "create",
        "commit_hash": "abc123",
        "branch": "main",
    }


def _allowlist_row(memory_id="mem_alice1", owner_id="alice", namespace="alice-ns"):
    """Routed-fetch row for the allowlist SELECT issued before sidecar
    imports. Mirrors what `_build_referenced_memory_allowlist` expects:
    a memories row with id + owner_id + namespace columns."""
    return {"id": memory_id, "owner_id": owner_id, "namespace": namespace}


def _cm_sidecar_entry(
    record_id: str = "mem_alice1",
    owner_id: str = "alice",
):
    return {
        "record_id": record_id,
        "engine_id": "apollo",
        "engine_version": "1.0",
        "compressed_content": "compressed:body",
        "compressed_tokens": 4,
        "compression_ratio": 2.5,
        "quality_score": 0.87,
        "composite_score": 0.81,
        "scoring_profile": "balanced",
        "judge_model": "claude-opus-4-7",
        "selected_at": "2026-01-01T00:00:00+00:00",
        "owner_id": owner_id,
    }


def test_import_kg_triples_sidecar_imports_with_caller_owner_for_non_root(monkeypatch):
    """Same anti-smuggling rule as memories — non-root rewrites
    owner_id + namespace on every kg_triple to the caller's identity."""
    # Allowlist SELECT after the records loop returns mem_alice1
    # under alice's identity (the records loop just inserted it).
    conn = _Conn(routed_rows={"FROM memories WHERE id = ANY": [_allowlist_row()]})
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[_memory_record(id="mem_alice1")],
        kg_triples=[_kg_sidecar_entry(owner_id="bob", namespace="bob-ns")],
    )
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 1
    assert stats.sidecars_imported == {"kg_triples": 1}

    kg_insert = next(e for e in conn.executes if "INSERT INTO kg_triples" in e[0])
    args = kg_insert[1]
    # Caller identity wins; the bob/bob-ns labels in the envelope are dropped.
    assert "alice" in args
    assert "alice-ns" in args
    assert "bob" not in args
    assert "bob-ns" not in args


def test_import_kg_triples_root_preserve_owner_honors_envelope(monkeypatch):
    # preserve_owner=True: sidecar's stated owner+ns must match the
    # referenced memory's actual owner+ns. Here the kg_triple claims
    # bob/bob-ns; the allowlist says mem_alice1 IS owned by bob/bob-ns
    # (root-driven cross-tenant migration scenario, not a smuggle).
    conn = _Conn(routed_rows={
        "FROM memories WHERE id = ANY": [
            _allowlist_row(owner_id="bob", namespace="bob-ns")
        ],
    })
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[_memory_record(id="mem_alice1")],
        kg_triples=[_kg_sidecar_entry(owner_id="bob", namespace="bob-ns")],
    )
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=True, user=_root(),
    ))
    assert stats.sidecars_imported == {"kg_triples": 1}
    kg_insert = next(e for e in conn.executes if "INSERT INTO kg_triples" in e[0])
    args = kg_insert[1]
    assert "bob" in args
    assert "bob-ns" in args


def test_import_kg_triples_missing_predicate_failed(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)

    bad = _kg_sidecar_entry()
    bad.pop("predicate")
    env = portability.MPFEnvelope(records=[], kg_triples=[bad])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.sidecars_imported == {}
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any("missing required" in e and "kg_triples" in e for e in stats.errors)
    # Critically: no INSERT executed for the malformed row.
    assert not any("INSERT INTO kg_triples" in e[0] for e in conn.executes)


def test_import_memory_versions_sidecar_imports(monkeypatch):
    conn = _Conn(routed_rows={
        "FROM memories WHERE id = ANY": [_allowlist_row()],
        # Post-import v1 verification SELECT — return mem_alice1 as
        # covered so the rollback path doesn't fire.
        "SELECT DISTINCT memory_id FROM memory_versions": [
            {"memory_id": "mem_alice1"},
        ],
    })
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[_memory_record(id="mem_alice1")],
        memory_versions=[_mv_sidecar_entry()],
    )
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.sidecars_imported == {"memory_versions": 1}
    mv_insert = next(e for e in conn.executes if "INSERT INTO memory_versions" in e[0])
    args = mv_insert[1]
    assert "mem_alice1" in args
    assert "abc123" in args  # commit_hash
    assert "main" in args    # branch


def test_import_memory_versions_missing_required_fails(monkeypatch):
    conn = _Conn()
    _install(monkeypatch, conn)

    bad = _mv_sidecar_entry()
    bad["content"] = ""  # required field empty
    env = portability.MPFEnvelope(records=[], memory_versions=[bad])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.sidecars_failed == {"memory_versions": 1}
    assert not any("INSERT INTO memory_versions" in e[0] for e in conn.executes)


def test_import_compression_manifest_sidecar_imports(monkeypatch):
    conn = _Conn(routed_rows={"FROM memories WHERE id = ANY": [_allowlist_row()]})
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[_memory_record(id="mem_alice1")],
        compression_manifest=[_cm_sidecar_entry()],
    )
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.sidecars_imported == {"compression_manifest": 1}
    cm_insert = next(e for e in conn.executes if "INSERT INTO memory_compressed_variants" in e[0])
    args = cm_insert[1]
    assert "mem_alice1" in args
    assert "apollo" in args
    # No namespace column on this table — caller's namespace must NOT
    # appear in the args list (only owner_id is bound).
    assert "alice-ns" not in args


def test_import_all_three_sidecars_under_one_envelope(monkeypatch):
    """Most realistic scenario: a CHARON round-trip envelope with one
    memory + a triple + a version + a compression entry. Per-surface
    counters break out cleanly."""
    conn = _Conn(routed_rows={
        "FROM memories WHERE id = ANY": [_allowlist_row()],
        "SELECT DISTINCT memory_id FROM memory_versions": [
            {"memory_id": "mem_alice1"},
        ],
    })
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[_memory_record(id="mem_alice1")],
        kg_triples=[_kg_sidecar_entry()],
        memory_versions=[_mv_sidecar_entry()],
        compression_manifest=[_cm_sidecar_entry()],
    )
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 1
    assert stats.sidecars_imported == {
        "kg_triples": 1,
        "memory_versions": 1,
        "compression_manifest": 1,
    }


def test_import_sidecar_idempotent_on_id_collision(monkeypatch):
    """Re-importing the same kg_triples / memory_versions /
    compression_manifest envelope is a no-op — counts as skipped,
    not imported."""
    class _DupeConn(_Conn):
        async def execute(self, sql, *args):
            self.executes.append((sql, args))
            return "INSERT 0 0"
    conn = _DupeConn(routed_rows={"FROM memories WHERE id = ANY": [_allowlist_row()]})
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[],
        kg_triples=[_kg_sidecar_entry()],
        memory_versions=[_mv_sidecar_entry()],
        compression_manifest=[_cm_sidecar_entry()],
    )
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.sidecars_imported == {}
    assert stats.sidecars_skipped == {
        "kg_triples": 1,
        "memory_versions": 1,
        "compression_manifest": 1,
    }


def test_import_no_sidecars_means_no_sidecar_inserts(monkeypatch):
    """A 0.1.0-shape envelope with sidecar fields absent must not
    trigger any sidecar INSERTs."""
    conn = _Conn()
    _install(monkeypatch, conn)

    env = _envelope([_memory_record(id="mem_alice1")])
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 1
    assert stats.sidecars_imported == {}
    assert not any("kg_triples" in e[0] or
                   "memory_versions" in e[0] or
                   "memory_compressed_variants" in e[0]
                   for e in conn.executes)


# ─── /v1/import — cross-tenant attachment defense (Codex finding #3) ────────


def test_import_kg_triple_referencing_foreign_memory_id_rejected(monkeypatch):
    """Attack scenario: alice posts a kg_triple with memory_id =
    'mem_bob_secret', a memory she does NOT own. The records loop
    skips bob's row via ON CONFLICT (id) DO NOTHING, but without the
    allowlist gate the kg_triple would attach to bob's memory under
    alice's owner_id+namespace, poisoning bob's read paths.

    Allowlist SELECT must return ZERO rows (alice does not own
    mem_bob_secret), and the helper must reject the entry, count it
    under sidecars_failed, and execute no INSERT against kg_triples."""
    conn = _Conn(routed_rows={
        # Allowlist returns nothing — alice owns no matching memory.
        "FROM memories WHERE id = ANY": [],
    })
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[],
        kg_triples=[_kg_sidecar_entry(id="kg_attack")],
    )
    # Override the entry's memory_id to point at bob.
    env.kg_triples[0]["memory_id"] = "mem_bob_secret"

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.sidecars_imported == {}
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any(
        "mem_bob_secret" in e and "not in caller-owned" in e
        for e in stats.errors
    ), f"expected rejection error, got {stats.errors}"
    assert not any("INSERT INTO kg_triples" in e[0] for e in conn.executes)


def test_import_memory_version_referencing_foreign_record_id_rejected(monkeypatch):
    """Same attack via memory_versions — alice tries to attach
    authoritative version history to bob's record_id."""
    conn = _Conn(routed_rows={
        "FROM memories WHERE id = ANY": [],
    })
    _install(monkeypatch, conn)

    bad = _mv_sidecar_entry()
    bad["record_id"] = "mem_bob_secret"
    env = portability.MPFEnvelope(records=[], memory_versions=[bad])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.sidecars_failed == {"memory_versions": 1}
    assert any(
        "mem_bob_secret" in e and "not in caller-owned" in e
        for e in stats.errors
    ), f"expected rejection error, got {stats.errors}"
    assert not any("INSERT INTO memory_versions" in e[0] for e in conn.executes)


def test_import_compression_manifest_referencing_foreign_record_id_rejected(monkeypatch):
    """Same attack via compression_manifest — would let alice plant
    arbitrary compressed_content + judge scores on bob's memory."""
    conn = _Conn(routed_rows={
        "FROM memories WHERE id = ANY": [],
    })
    _install(monkeypatch, conn)

    bad = _cm_sidecar_entry()
    bad["record_id"] = "mem_bob_secret"
    env = portability.MPFEnvelope(records=[], compression_manifest=[bad])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.sidecars_failed == {"compression_manifest": 1}
    assert any(
        "mem_bob_secret" in e and "not in caller-owned" in e
        for e in stats.errors
    ), f"expected rejection error, got {stats.errors}"
    assert not any(
        "INSERT INTO memory_compressed_variants" in e[0] for e in conn.executes
    )


def test_import_kg_triple_with_no_memory_id_is_first_class(monkeypatch):
    """A kg_triple without memory_id is a stand-alone fact (e.g.
    Graphiti-style first-class triple). It should NOT be rejected
    by the allowlist gate — there's no memory FK to validate."""
    conn = _Conn(routed_rows={
        "FROM memories WHERE id = ANY": [],
    })
    _install(monkeypatch, conn)

    free = _kg_sidecar_entry(id="kg_first_class")
    free.pop("memory_id")  # first-class triple
    env = portability.MPFEnvelope(records=[], kg_triples=[free])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.sidecars_imported == {"kg_triples": 1}
    assert stats.sidecars_failed == {}


def test_import_preserve_owner_rejects_owner_namespace_mismatch(monkeypatch):
    """Under preserve_owner=true: sidecar's stated owner+ns MUST
    match the referenced memory's actual owner+ns. If a root caller
    posts a kg_triple stamped owner=bob+ns=bob-ns referencing a
    memory_id that DB says is owned by carol, reject."""
    conn = _Conn(routed_rows={
        "FROM memories WHERE id = ANY": [
            _allowlist_row(memory_id="mem_carol_real",
                           owner_id="carol", namespace="carol-ns")
        ],
    })
    _install(monkeypatch, conn)

    bad = _kg_sidecar_entry(owner_id="bob", namespace="bob-ns")
    bad["memory_id"] = "mem_carol_real"
    env = portability.MPFEnvelope(records=[], kg_triples=[bad])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=True, user=_root(),
    ))
    assert stats.sidecars_failed == {"kg_triples": 1}
    assert any(
        "mem_carol_real" in e and "carol" in e.lower()
        for e in stats.errors
    ), f"expected owner-mismatch rejection, got {stats.errors}"


def test_import_compression_manifest_cross_namespace_same_owner_rejected(monkeypatch):
    """Codex review #2: alice in ns_A submits a compression_manifest
    referencing alice's OWN memory in ns_B. The previous fix passed
    require_namespace_match=False because the variants table has no
    namespace column — but the threat model is that compressed
    content in ns_B gets poisoned by alice acting from ns_A.

    Validation must use the referenced memory's namespace, not the
    variants table's lack of one. Allowlist returns mem_alice_in_B
    as alice/ns_B; the caller is alice/ns_A — namespace mismatch
    must reject."""
    conn = _Conn(routed_rows={
        "FROM memories WHERE id = ANY": [
            _allowlist_row(memory_id="mem_alice_in_B",
                           owner_id="alice", namespace="ns_B"),
        ],
    })
    _install(monkeypatch, conn)

    bad = _cm_sidecar_entry()
    bad["record_id"] = "mem_alice_in_B"
    env = portability.MPFEnvelope(records=[], compression_manifest=[bad])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False,
        user=UserContext(
            user_id="alice", group_ids=[], role="user",
            namespace="ns_A", authenticated=True,
        ),
    ))
    assert stats.sidecars_failed == {"compression_manifest": 1}
    assert any(
        "ns_B" in e or "ns_A" in e
        for e in stats.errors
    ), f"expected ns mismatch rejection, got {stats.errors}"
    assert not any(
        "INSERT INTO memory_compressed_variants" in e[0] for e in conn.executes
    )


def test_import_partial_memory_versions_coverage_rejected(monkeypatch):
    """Codex review #2: if envelope ships memory_versions sidecar
    that doesn't cover every kind:memory record, the import must
    reject upfront (before any trigger suppression). Otherwise
    records without coverage land in `memories` with no v1 — the
    trigger is suppressed and the sidecar has no entry."""
    conn = _Conn()
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[
            _memory_record(id="mem_covered"),
            _memory_record(id="mem_uncovered"),
        ],
        memory_versions=[
            # Only mem_covered has a v1; mem_uncovered does not.
            {**_mv_sidecar_entry(), "record_id": "mem_covered"},
        ],
    )

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portability.import_memories(
            envelope=env, preserve_owner=False, user=_alice(),
        ))
    assert exc.value.status_code == 400
    assert "memory_versions sidecar must cover" in exc.value.detail
    assert "mem_uncovered" in exc.value.detail
    # Critically: NO INSERTs executed — we rejected before opening
    # the transaction.
    assert not any("INSERT INTO" in e[0] for e in conn.executes)


def test_import_full_memory_versions_coverage_passes(monkeypatch):
    """Companion to the partial-coverage rejection test: when every
    record HAS a v1 in the sidecar, the import proceeds normally."""
    conn = _Conn(routed_rows={
        "FROM memories WHERE id = ANY": [
            _allowlist_row(memory_id="mem_a"),
            _allowlist_row(memory_id="mem_b"),
        ],
        "SELECT DISTINCT memory_id FROM memory_versions": [
            {"memory_id": "mem_a"}, {"memory_id": "mem_b"},
        ],
    })
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[
            _memory_record(id="mem_a"),
            _memory_record(id="mem_b"),
        ],
        memory_versions=[
            {**_mv_sidecar_entry(), "record_id": "mem_a",
             "id": "00000000-0000-0000-0000-000000000aaa"},
            {**_mv_sidecar_entry(), "record_id": "mem_b",
             "id": "00000000-0000-0000-0000-000000000bbb"},
        ],
    )
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 2
    assert stats.sidecars_imported.get("memory_versions") == 2


def test_import_memory_versions_restores_branch_head(monkeypatch):
    """Codex review #2: after memory_versions sidecar import, the
    handler must upsert memory_branches with the head version_id
    per (memory_id, branch). The trigger normally does this on
    memory INSERT but is suppressed during CHARON imports.

    Verify by checking that an INSERT INTO memory_branches was
    executed for each imported memory_id."""
    conn = _Conn(routed_rows={
        "FROM memories WHERE id = ANY": [_allowlist_row()],
        # _restore_memory_branches issues a DISTINCT ON SELECT
        # against memory_versions — return the v1 row we'd expect
        # post-import.
        "SELECT DISTINCT ON (memory_id, branch)": [
            {"memory_id": "mem_alice1", "branch": "main",
             "head_version_id": "11111111-1111-1111-1111-111111111111"},
        ],
        # Post-import v1 verification needs to find the imported memory.
        "SELECT DISTINCT memory_id FROM memory_versions": [
            {"memory_id": "mem_alice1"},
        ],
    })
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[_memory_record(id="mem_alice1")],
        memory_versions=[_mv_sidecar_entry()],
    )
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.sidecars_imported.get("memory_versions") == 1
    # memory_branches UPSERT must have been executed
    branch_inserts = [e for e in conn.executes if "INSERT INTO memory_branches" in e[0]]
    assert len(branch_inserts) == 1, (
        f"expected one memory_branches UPSERT, got {len(branch_inserts)}: "
        f"{[e[0][:60] for e in conn.executes]}"
    )
    args = branch_inserts[0][1]
    assert "mem_alice1" in args
    assert "main" in args


def test_import_post_verification_rolls_back_when_memory_unversioned(monkeypatch):
    """Codex review #3: even with full pre-coverage, a per-row
    failure in memory_versions sidecar (e.g. allowlist rejection or
    UUID format error) can leave a memory committed without v1
    under trigger suppression. Post-import verification must SELECT
    memory_versions and rollback if any imported memory is uncovered."""
    conn = _Conn(routed_rows={
        # Allowlist returns the memory under a DIFFERENT owner so the
        # sidecar entry's allowlist check fails — the memory record
        # itself still inserts (the records loop runs first).
        "FROM memories WHERE id = ANY": [
            _allowlist_row(memory_id="mem_alice1",
                           owner_id="charlie", namespace="charlie-ns"),
        ],
        # Post-verification SELECT: memory exists in DB...
        # but coverage SELECT returns NOTHING — uncovered.
        "SELECT DISTINCT memory_id FROM memory_versions": [],
    })
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[_memory_record(id="mem_alice1")],
        memory_versions=[_mv_sidecar_entry()],
    )
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portability.import_memories(
            envelope=env, preserve_owner=False, user=_alice(),
        ))
    assert exc.value.status_code == 500
    assert "without version history" in exc.value.detail
    assert "mem_alice1" in exc.value.detail


def test_import_post_verification_ignores_pre_existing_uncovered_memories(monkeypatch):
    """Codex review #4: post-verification must scope to records THIS
    request actually INSERTed, not the full envelope.records list.
    Otherwise a pre-existing legacy memory with no v1 history (which
    this transaction did not create) could roll back an unrelated
    import. _DupeConn returns INSERT 0 0 for everything → nothing
    new was inserted → no post-verification rollback even though
    the coverage SELECT comes back empty."""
    class _DupeConn(_Conn):
        async def execute(self, sql, *args):
            self.executes.append((sql, args))
            return "INSERT 0 0"
    conn = _DupeConn(routed_rows={
        "FROM memories WHERE id = ANY": [_allowlist_row()],
        # Coverage SELECT: returns empty (the pre-existing memory has
        # no v1 — legacy data). Without the inserted-set scope, this
        # would trigger the 500 rollback.
        "SELECT DISTINCT memory_id FROM memory_versions": [],
    })
    _install(monkeypatch, conn)

    env = portability.MPFEnvelope(
        records=[_memory_record(id="mem_alice1")],
        memory_versions=[_mv_sidecar_entry()],
    )
    # No HTTPException — conflict means we didn't insert, so verification skips.
    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.imported == 0
    assert stats.skipped == 1
    # Sidecar imports still ran but landed under skipped-via-conflict
    # (the dupe conn returns INSERT 0 0 for everything).
    assert stats.sidecars_skipped.get("memory_versions") == 1


def test_import_branch_restore_skips_rejected_record_ids(monkeypatch):
    """Codex review #3: if a memory_versions sidecar entry is
    rejected by the allowlist gate, _restore_memory_branches must
    NOT issue an UPSERT for that record_id — even if the underlying
    DB has prior versions for it. Otherwise an adversarial envelope
    could trigger writes against another tenant's memory_branches."""
    conn = _Conn(routed_rows={
        # Allowlist: the only memory we know about is owned by bob,
        # so alice's sidecar entry referencing mem_bob_secret will
        # fail the allowlist check.
        "FROM memories WHERE id = ANY": [
            _allowlist_row(memory_id="mem_bob_secret",
                           owner_id="bob", namespace="bob-ns"),
        ],
        # Post-verification: alice's records loop didn't insert
        # anything (no records in envelope), so this returns empty.
        "SELECT DISTINCT memory_id FROM memory_versions": [],
        # Branch restore would issue a SELECT DISTINCT ON if called.
        "SELECT DISTINCT ON (memory_id, branch)": [
            {"memory_id": "mem_bob_secret", "branch": "main",
             "head_version_id": "ffffffff-ffff-ffff-ffff-ffffffffffff"},
        ],
    })
    _install(monkeypatch, conn)

    bad = _mv_sidecar_entry()
    bad["record_id"] = "mem_bob_secret"
    env = portability.MPFEnvelope(records=[], memory_versions=[bad])

    stats = asyncio.run(portability.import_memories(
        envelope=env, preserve_owner=False, user=_alice(),
    ))
    assert stats.sidecars_failed == {"memory_versions": 1}
    # The rejected entry must NOT have driven a memory_branches UPSERT.
    branch_inserts = [
        e for e in conn.executes if "INSERT INTO memory_branches" in e[0]
    ]
    assert branch_inserts == [], (
        f"expected no memory_branches UPSERT for rejected entry; "
        f"got {len(branch_inserts)}: {[e[0][:60] for e in branch_inserts]}"
    )
