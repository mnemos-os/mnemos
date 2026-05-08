"""Slice #205: pin two LOW audit fixes from
mem_1778221719390_8cb1ba.

(a) ``mnemos/api/routes/webhooks.py:list_deliveries`` ``limit``
    parameter — caller-controlled, no API-side bound. Capped at
    200 with FastAPI ``Query(50, ge=1, le=200)``.

(b) ``model_registry.capabilities @> $3`` (in
    ``providers.py:106``) had no GIN index — degraded to
    seq-scan as registry grew. Added via migration
    ``migrations_v5_3_5_model_registry_capabilities_gin.sql``,
    registered in the installer canonical loader.

Tests don't need a live Postgres — they verify the source-level
shape of both fixes (FastAPI Query bounds + migration file
present + installer registration).
"""
from __future__ import annotations

import inspect
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_list_deliveries_limit_is_bounded():
    """The list_deliveries route must declare its `limit`
    parameter via FastAPI ``Query(...)`` with explicit ``ge`` and
    ``le`` bounds. Bare ``limit: int = 50`` would silently allow
    a caller to ask for an effectively unlimited page."""
    from mnemos.api.routes import webhooks
    sig = inspect.signature(webhooks.list_deliveries)
    limit = sig.parameters.get("limit")
    assert limit is not None
    default = limit.default
    # FastAPI Query(...) returns a special FieldInfo — check it
    # has the metadata we expect rather than testing the type
    # directly.
    metadata = getattr(default, "metadata", None)
    if metadata is None:
        # Fallback: pydantic FieldInfo exposes ge/le via
        # metadata constraint objects in v2; older shape uses
        # `default.ge` / `default.le` directly.
        ge = getattr(default, "ge", None)
        le = getattr(default, "le", None)
    else:
        ge = next((m.ge for m in metadata if hasattr(m, "ge")), None)
        le = next((m.le for m in metadata if hasattr(m, "le")), None)
    assert ge == 1, (
        "list_deliveries limit lost its ge=1 bound; this lets "
        "callers ask for limit=0 / negative limits, which goes "
        "into the SQL LIMIT clause as-is."
    )
    assert le == 200, (
        f"list_deliveries limit upper bound is {le!r}, expected "
        "200. If the cap moved, update this test alongside."
    )


def test_capabilities_gin_migration_file_exists():
    """The migration file that adds the GIN index must exist on
    disk under db/. Without it the installer's canonical loader
    would fail with a missing-file error."""
    p = (REPO / "db"
         / "migrations_v5_3_5_model_registry_capabilities_gin.sql")
    assert p.exists(), (
        "migrations_v5_3_5_model_registry_capabilities_gin.sql "
        "is missing — the installer registration in "
        "mnemos/installer/db.py would fail at first install."
    )
    src = p.read_text()
    # Pin the actual SQL we expect — GIN index over the
    # `capabilities` column. Without the GIN, `@>` containment
    # falls back to seq-scan.
    assert "USING GIN" in src, (
        "migration no longer creates a GIN index. btree won't "
        "match the `@>` containment predicate; GIN is the right "
        "shape for TEXT[] containment under PostgreSQL."
    )
    assert "model_registry" in src
    assert "capabilities" in src
    assert "IF NOT EXISTS" in src, (
        "migration must be idempotent (CREATE INDEX IF NOT EXISTS) "
        "so a re-run on already-migrated clusters is a no-op."
    )


def test_capabilities_gin_migration_registered_in_installer():
    """The canonical migration list in mnemos/installer/db.py
    must include the new migration. The migrations are applied
    in-order; the installer ignores files not in the list."""
    src = (REPO / "mnemos" / "installer" / "db.py").read_text()
    assert "migrations_v5_3_5_model_registry_capabilities_gin.sql" in src, (
        "The new GIN-index migration is not registered in the "
        "installer canonical loader. Without registration, fresh "
        "installs would never apply the index even though the SQL "
        "file is on disk."
    )


def test_capabilities_gin_migration_appears_after_v5_3_4():
    """Migration ordering matters — apply later than 5.3.4 so
    pre-existing schemas don't get the index before tables
    they depend on."""
    src = (REPO / "mnemos" / "installer" / "db.py").read_text()
    v534 = src.find("migrations_v5_3_4_mcp_audit_log.sql")
    v535 = src.find("migrations_v5_3_5_model_registry_capabilities_gin.sql")
    assert v534 != -1
    assert v535 != -1
    assert v535 > v534, (
        "v5.3.5 migration is registered before v5.3.4 in the "
        "ordered migration list — that's a left-shift bug. "
        "The installer applies in list order."
    )
