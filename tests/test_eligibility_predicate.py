from __future__ import annotations

from mnemos.db.eligibility import (
    MEMORY_ELIGIBILITY_PREDICATE,
    eligible_for_compression,
    eligible_for_federation,
    eligible_for_morpheus,
)


ROWS = [
    {"id": "active", "deleted_at": None, "archived_at": None, "consolidated_into": None, "permission_mode": 644},
    {"id": "deleted", "deleted_at": "ts", "archived_at": None, "consolidated_into": None, "permission_mode": 644},
    {"id": "archived", "deleted_at": None, "archived_at": "ts", "consolidated_into": None, "permission_mode": 644},
    {"id": "consolidated", "deleted_at": None, "archived_at": None, "consolidated_into": "active", "permission_mode": 644},
    {"id": "private_parent", "deleted_at": None, "archived_at": None, "consolidated_into": None, "permission_mode": 400},
]


def _canonical_ids(rows=ROWS) -> list[str]:
    return [
        row["id"]
        for row in rows
        if row["deleted_at"] is None
        and row["archived_at"] is None
        and row["consolidated_into"] is None
    ]


def test_canonical_memory_eligibility_predicate_filters_universal_exclusions():
    assert MEMORY_ELIGIBILITY_PREDICATE == (
        "deleted_at IS NULL AND archived_at IS NULL AND consolidated_into IS NULL"
    )
    assert _canonical_ids() == ["active", "private_parent"]
    assert eligible_for_morpheus("m") == (
        "m.deleted_at IS NULL AND m.archived_at IS NULL "
        "AND m.consolidated_into IS NULL"
    )


def test_compression_eligibility_rejects_private_consolidation_parent():
    selected = [
        row["id"]
        for row in ROWS
        if row["id"] in _canonical_ids() and row["permission_mode"] != 400
    ]

    assert selected == ["active"]
    assert eligible_for_compression("m", reject_private_parent=True).endswith(
        "AND m.permission_mode <> 400"
    )


def test_federation_eligibility_rejects_archived_consolidated_and_private_rows():
    selected = [
        row["id"]
        for row in ROWS
        if row["id"] in _canonical_ids() and (row["permission_mode"] % 10) >= 4
    ]

    assert selected == ["active"]
    predicate = eligible_for_federation("m")
    assert "m.archived_at IS NULL" in predicate
    assert "m.consolidated_into IS NULL" in predicate
    assert "(m.permission_mode % 10) >= 4" in predicate


def test_callsite_queries_reference_shared_predicates():
    from mnemos.api.routes.federation import _federation_visibility_filters
    from mnemos.domain.compression.contest_store import _FETCH_SOURCE_MAIN_HEAD_SQL
    from mnemos.domain.compression.worker_contest import _MEMORY_CONTENT_SQL

    assert _federation_visibility_filters() == [eligible_for_federation("m")]
    assert eligible_for_compression("", reject_private_parent=True) in _MEMORY_CONTENT_SQL
    assert eligible_for_compression("m", reject_private_parent=True) in _FETCH_SOURCE_MAIN_HEAD_SQL
