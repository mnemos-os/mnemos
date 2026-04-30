"""SQL repository for MPF import/export.

Domain portability modules own validation and orchestration. This
module owns every SELECT / INSERT / UPDATE / DELETE used by that flow.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence


async def fetch_memory_export(
    conn,
    *,
    effective_owner: Optional[str],
    effective_ns: Optional[str],
    category: Optional[str],
    limit: int,
    offset: int,
):
    conditions: list[str] = []
    params: list[Any] = []
    idx = 1
    if effective_owner:
        conditions.append(f"owner_id = ${idx}")
        params.append(effective_owner)
        idx += 1
    if effective_ns:
        conditions.append(f"namespace = ${idx}")
        params.append(effective_ns)
        idx += 1
    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = (
        "SELECT id, content, category, subcategory, created, updated, "
        "owner_id, namespace, permission_mode, quality_rating, "
        "source_model, source_provider, source_session, source_agent, "
        "metadata "
        "FROM memories "
        f"{where} "
        f"ORDER BY created ASC "
        f"LIMIT ${idx} OFFSET ${idx + 1}"
    )
    params.extend([limit, offset])
    return await conn.fetch(sql, *params)


async def _fetch_sidecar(
    conn,
    *,
    table: str,
    columns: str,
    memory_id_column: str,
    memory_ids: Sequence[str],
    effective_owner: Optional[str],
    effective_ns: Optional[str],
    bound_to_memories: bool,
    hard_limit: int,
    null_ok: bool = False,
    order_by: Optional[str] = None,
):
    if bound_to_memories and not memory_ids and not null_ok:
        return []

    conditions: list[str] = []
    params: list[Any] = []
    idx = 1
    if bound_to_memories:
        if null_ok and memory_ids:
            conditions.append(
                f"({memory_id_column} IS NULL OR {memory_id_column} = ANY(${idx}::text[]))"
            )
            params.append(list(memory_ids))
            idx += 1
        elif null_ok:
            conditions.append(f"{memory_id_column} IS NULL")
        else:
            conditions.append(f"{memory_id_column} = ANY(${idx}::text[])")
            params.append(list(memory_ids))
            idx += 1
    if effective_owner:
        conditions.append(f"owner_id = ${idx}")
        params.append(effective_owner)
        idx += 1
    if effective_ns:
        conditions.append(f"namespace = ${idx}")
        params.append(effective_ns)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    order = f"ORDER BY {order_by}" if order_by else ""
    sql = f"SELECT {columns} FROM {table} {where} {order} LIMIT {hard_limit + 1}"
    return await conn.fetch(sql, *params)


async def fetch_kg_triples_for_export(
    conn,
    *,
    memory_ids: Sequence[str],
    effective_owner: Optional[str],
    effective_ns: Optional[str],
    include_unattached: bool,
    hard_limit: int,
):
    return await _fetch_sidecar(
        conn,
        table="kg_triples",
        columns=(
            "id, subject, predicate, object, subject_type, "
            "object_type, valid_from, valid_until, memory_id, "
            "confidence, created, owner_id, namespace"
        ),
        memory_id_column="memory_id",
        memory_ids=memory_ids,
        effective_owner=effective_owner,
        effective_ns=effective_ns,
        bound_to_memories=True,
        hard_limit=hard_limit,
        null_ok=include_unattached,
    )


async def fetch_memory_versions_for_export(
    conn,
    *,
    memory_ids: Sequence[str],
    effective_owner: Optional[str],
    effective_ns: Optional[str],
    hard_limit: int,
):
    return await _fetch_sidecar(
        conn,
        table="memory_versions",
        columns=(
            "id, memory_id, version_num, content, category, "
            "subcategory, metadata, verbatim_content, owner_id, "
            "namespace, permission_mode, source_model, source_provider, "
            "source_session, source_agent, snapshot_at, snapshot_by, "
            "change_type, commit_hash, parent_version_id, branch, "
            "merge_parents"
        ),
        memory_id_column="memory_id",
        memory_ids=memory_ids,
        effective_owner=effective_owner,
        effective_ns=effective_ns,
        bound_to_memories=True,
        hard_limit=hard_limit,
        order_by="memory_id ASC, branch ASC, version_num ASC",
    )


async def fetch_compressed_variants_for_export(
    conn,
    *,
    memory_ids: Sequence[str],
    effective_owner: Optional[str],
    hard_limit: int,
):
    return await _fetch_sidecar(
        conn,
        table="memory_compressed_variants",
        columns=(
            "memory_id, owner_id, winner_candidate_id, engine_id, "
            "engine_version, compressed_content, compressed_tokens, "
            "compression_ratio, quality_score, composite_score, "
            "scoring_profile, judge_model, selected_at"
        ),
        memory_id_column="memory_id",
        memory_ids=memory_ids,
        effective_owner=effective_owner,
        effective_ns=None,
        bound_to_memories=True,
        hard_limit=hard_limit,
    )


async def fetch_referenced_memory_allowlist(
    conn,
    *,
    referenced_ids: Sequence[str],
    scope_owner: Optional[str] = None,
    scope_namespace: Optional[str] = None,
):
    sql = "SELECT id, owner_id, namespace FROM memories WHERE id = ANY($1::text[])"
    params: list[Any] = [list(referenced_ids)]
    if scope_owner is not None:
        sql += " AND owner_id = $2"
        params.append(scope_owner)
        if scope_namespace is not None:
            sql += " AND namespace = $3"
            params.append(scope_namespace)
    elif scope_namespace is not None:
        sql += " AND namespace = $2"
        params.append(scope_namespace)
    return await conn.fetch(sql, *params)


async def insert_memory(
    conn,
    *,
    memory_id: str,
    content: str,
    category: str,
    subcategory: Optional[str],
    metadata_json: str,
    quality_rating: int,
    owner_id: str,
    namespace: str,
    permission_mode: int,
    source_model: Optional[str],
    source_provider: Optional[str],
    source_session: Optional[str],
    source_agent: Optional[str],
    verbatim_content: Optional[str],
    created,
    updated,
) -> str:
    return await conn.execute(
        """
        INSERT INTO memories (
            id, content, category, subcategory, metadata,
            quality_rating, verbatim_content, owner_id, namespace, permission_mode,
            source_model, source_provider, source_session, source_agent,
            created, updated
        )
        VALUES (
            $1, $2, $3, $4, $5::jsonb,
            $6, $7, $8, $9, $10,
            $11, $12, $13, $14,
            COALESCE($15, NOW()), COALESCE($16, NOW())
        )
        ON CONFLICT (id) DO NOTHING
        """,
        memory_id,
        content,
        category,
        subcategory,
        metadata_json,
        quality_rating,
        verbatim_content,
        owner_id,
        namespace,
        permission_mode,
        source_model,
        source_provider,
        source_session,
        source_agent,
        created,
        updated,
    )


async def fetch_memory_by_id(conn, memory_id: str):
    return await conn.fetchrow(
        "SELECT content, category, subcategory, "
        "metadata, quality_rating, owner_id, "
        "namespace, permission_mode, "
        "source_model, source_provider, "
        "source_session, source_agent, "
        "created, updated "
        "FROM memories WHERE id = $1",
        memory_id,
    )


async def set_suppress_version_snapshot(conn) -> None:
    await conn.execute("SET LOCAL mnemos.suppress_version_snapshot = '1'")


async def delete_memory_branches_for_memories(conn, memory_ids: Sequence[str]) -> None:
    await conn.execute(
        "DELETE FROM memory_branches WHERE memory_id = ANY($1::text[])",
        list(memory_ids),
    )


async def fetch_versioned_memory_ids(conn, memory_ids: Sequence[str]):
    return await conn.fetch(
        "SELECT DISTINCT memory_id FROM memory_versions "
        "WHERE memory_id = ANY($1::text[])",
        list(memory_ids),
    )


async def fetch_memory_head_checks(conn, memory_ids: Sequence[str]):
    return await conn.fetch(
        """
        SELECT m.id, m.content AS memory_content,
               mv.content AS head_content
        FROM memories m
        LEFT JOIN memory_branches b
          ON b.memory_id = m.id AND b.name = 'main'
        LEFT JOIN memory_versions mv
          ON mv.id = b.head_version_id
        WHERE m.id = ANY($1::text[])
        """,
        list(memory_ids),
    )


async def insert_kg_triple(
    conn,
    *,
    triple_id: str,
    subject: str,
    predicate: str,
    obj: str,
    subject_type: Optional[str],
    object_type: Optional[str],
    valid_from,
    valid_until,
    memory_id: Optional[str],
    confidence: Optional[float],
    created,
    owner_id: str,
    namespace: Optional[str],
) -> str:
    return await conn.execute(
        """
        INSERT INTO kg_triples (
            id, subject, predicate, object,
            subject_type, object_type,
            valid_from, valid_until,
            memory_id, confidence, created,
            owner_id, namespace
        )
        VALUES (
            $1, $2, $3, $4,
            $5, $6,
            COALESCE($7, NOW()), $8,
            $9, COALESCE($10, 1.0),
            COALESCE($11, NOW()),
            $12, $13
        )
        ON CONFLICT (id) DO NOTHING
        """,
        triple_id,
        subject,
        predicate,
        obj,
        subject_type,
        object_type,
        valid_from,
        valid_until,
        memory_id,
        confidence,
        created,
        owner_id,
        namespace,
    )


async def fetch_kg_triple_by_id(conn, triple_id: str):
    return await conn.fetchrow(
        "SELECT subject, predicate, object, subject_type, "
        "object_type, memory_id, confidence, owner_id, "
        "namespace, valid_from, valid_until, created "
        "FROM kg_triples WHERE id = $1",
        triple_id,
    )


async def fetch_memory_branch_heads(
    conn,
    memory_ids: Sequence[str],
    *,
    authorized_version_uuids: Optional[Sequence[str]] = None,
):
    if authorized_version_uuids is not None:
        return await conn.fetch(
            """
            SELECT DISTINCT ON (memory_id, branch)
                memory_id, branch, id AS head_version_id
            FROM memory_versions
            WHERE memory_id = ANY($1::text[])
              AND id = ANY($2::uuid[])
            ORDER BY memory_id, branch, version_num DESC
            """,
            list(memory_ids),
            list(authorized_version_uuids),
        )
    return await conn.fetch(
        """
        SELECT DISTINCT ON (memory_id, branch)
            memory_id, branch, id AS head_version_id
        FROM memory_versions
        WHERE memory_id = ANY($1::text[])
        ORDER BY memory_id, branch, version_num DESC
        """,
        list(memory_ids),
    )


async def upsert_memory_branch_head(
    conn,
    *,
    memory_id: str,
    branch: str,
    head_version_id,
) -> None:
    await conn.execute(
        """
        INSERT INTO memory_branches (memory_id, name, head_version_id, created_by)
        VALUES ($1, $2, $3, NULL)
        ON CONFLICT (memory_id, name) DO UPDATE
        SET head_version_id = EXCLUDED.head_version_id
        """,
        memory_id,
        branch,
        head_version_id,
    )


async def fetch_memory_versions_by_ids(conn, version_ids: Sequence[str]):
    return await conn.fetch(
        "SELECT id::text AS id, memory_id, owner_id, namespace "
        "FROM memory_versions WHERE id = ANY($1::uuid[])",
        list(version_ids),
    )


async def insert_memory_version(
    conn,
    *,
    version_id: str,
    memory_id: str,
    version_num: int,
    content: str,
    category: Optional[str],
    subcategory: Optional[str],
    metadata_json: str,
    verbatim_content: Optional[str],
    owner_id: str,
    namespace: Optional[str],
    permission_mode: Optional[int],
    source_model: Optional[str],
    source_provider: Optional[str],
    source_session: Optional[str],
    source_agent: Optional[str],
    snapshot_at,
    snapshot_by: Optional[str],
    change_type: Optional[str],
    commit_hash: Optional[str],
    parent_version_id: Optional[str],
    branch: Optional[str],
    merge_parents,
) -> str:
    return await conn.execute(
        """
        INSERT INTO memory_versions (
            id, memory_id, version_num, content,
            category, subcategory, metadata, verbatim_content,
            owner_id, namespace, permission_mode,
            source_model, source_provider, source_session, source_agent,
            snapshot_at, snapshot_by, change_type,
            commit_hash, parent_version_id, branch, merge_parents
        )
        VALUES (
            $1::uuid, $2, $3, $4,
            $5, $6, $7::jsonb, $8,
            $9, $10, COALESCE($11, 600),
            $12, $13, $14, $15,
            COALESCE($16, NOW()), $17, COALESCE($18, 'create'),
            $19, $20::uuid, COALESCE($21, 'main'), $22::uuid[]
        )
        ON CONFLICT (id) DO NOTHING
        """,
        version_id,
        memory_id,
        version_num,
        content,
        category,
        subcategory,
        metadata_json,
        verbatim_content,
        owner_id,
        namespace,
        permission_mode,
        source_model,
        source_provider,
        source_session,
        source_agent,
        snapshot_at,
        snapshot_by,
        change_type,
        commit_hash,
        parent_version_id,
        branch,
        merge_parents,
    )


async def fetch_memory_version_by_id(conn, version_id: str):
    return await conn.fetchrow(
        "SELECT memory_id, owner_id, namespace, "
        "version_num, content, commit_hash, "
        "parent_version_id::text AS parent_version_id, "
        "branch, merge_parents, category, subcategory, "
        "metadata, verbatim_content, permission_mode, "
        "source_model, source_provider, source_session, "
        "source_agent, snapshot_at, snapshot_by, "
        "change_type "
        "FROM memory_versions WHERE id = $1::uuid",
        version_id,
    )


async def compression_candidate_exists(
    conn,
    *,
    candidate_id: str,
    memory_id: str,
    owner_id: str,
) -> bool:
    exists = await conn.fetchval(
        "SELECT 1 FROM memory_compression_candidates "
        "WHERE id = $1::uuid AND memory_id = $2 "
        "AND owner_id = $3",
        candidate_id,
        memory_id,
        owner_id,
    )
    return bool(exists)


async def insert_compressed_variant(
    conn,
    *,
    memory_id: str,
    owner_id: str,
    winner_candidate_id: Optional[str],
    engine_id: str,
    engine_version: Optional[str],
    compressed_content: Optional[str],
    compressed_tokens: Optional[int],
    compression_ratio: Optional[float],
    quality_score: Optional[float],
    composite_score: Optional[float],
    scoring_profile: Optional[str],
    judge_model: Optional[str],
    selected_at,
) -> str:
    return await conn.execute(
        """
        INSERT INTO memory_compressed_variants (
            memory_id, owner_id, winner_candidate_id,
            engine_id, engine_version, compressed_content,
            compressed_tokens, compression_ratio,
            quality_score, composite_score,
            scoring_profile, judge_model, selected_at
        )
        VALUES (
            $1, $2, $3::uuid,
            $4, $5, $6,
            $7, $8,
            $9, $10,
            COALESCE($11, 'balanced'), $12,
            COALESCE($13, NOW())
        )
        ON CONFLICT (memory_id) DO NOTHING
        """,
        memory_id,
        owner_id,
        winner_candidate_id,
        engine_id,
        engine_version,
        compressed_content,
        compressed_tokens,
        compression_ratio,
        quality_score,
        composite_score,
        scoring_profile,
        judge_model,
        selected_at,
    )


async def fetch_compressed_variant_by_memory_id(conn, memory_id: str):
    return await conn.fetchrow(
        "SELECT owner_id, winner_candidate_id::text "
        "AS winner_candidate_id, engine_id, "
        "engine_version, compressed_content, "
        "compressed_tokens, compression_ratio, "
        "quality_score, composite_score, "
        "scoring_profile, judge_model, selected_at "
        "FROM memory_compressed_variants "
        "WHERE memory_id = $1",
        memory_id,
    )
