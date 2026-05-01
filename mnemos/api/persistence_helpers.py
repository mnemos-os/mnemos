"""Shared backend / RLS helpers for the route handlers.

Hosts the helpers that multiple route modules need so they can stay
behaviourally identical without growing a route-to-route import cycle.

The helpers are deliberately small: a 503 wrapper around the active
persistence backend, and an RLS-context applier that no-ops on
SQLite and on unauthenticated calls. Anything richer than that
should grow in a domain or persistence module instead.
"""
from __future__ import annotations

from fastapi import HTTPException

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext


def backend_or_503():
    """Return the active persistence backend or raise 503."""
    backend = _lc._persistence_backend
    if backend is None:
        raise HTTPException(
            status_code=503,
            detail="Persistence backend not available",
        )
    return backend


def require_postgres_pool_or_503(*, route_label: str = "this endpoint"):
    """Return the asyncpg pool or raise a 503 with profile-aware
    detail.

    Many legacy routes use raw asyncpg SQL and hard-require
    ``_lc._pool``. On SQLite/edge profiles the lifecycle sets
    ``_pool = None`` deliberately — those routes are
    Postgres-only-by-design, not transiently down. Distinguishing
    the two failure modes in the 503 detail saves operators on
    edge profiles from chasing a phantom outage.

    Returns the pool for caller use. Raises HTTPException(503) with:
      * "endpoint requires Postgres backend ..." when the active
        persistence backend is SQLite (the route is fundamentally
        unsupported on this profile).
      * "Database pool not available" when the backend is Postgres
        but the pool isn't (transient — startup race or pool
        terminated mid-request).
    """
    pool = _lc._pool
    if pool is not None:
        return pool

    # Pool is None. Decide whether that's a profile artefact or a
    # transient outage.
    backend = _lc._persistence_backend
    backend_kind = type(backend).__name__ if backend is not None else None
    if backend_kind and "Sqlite" in backend_kind:
        raise HTTPException(
            status_code=503,
            detail=(
                f"{route_label} requires the Postgres backend; "
                f"this deployment is configured with SQLite. "
                f"Set MNEMOS_PROFILE=server (or MNEMOS_PERSISTENCE_BACKEND="
                f"postgres + a working PG_* / MNEMOS_DATABASE_URL) to "
                f"enable Postgres-only routes."
            ),
        )
    raise HTTPException(
        status_code=503,
        detail="Database pool not available",
    )


async def maybe_set_pg_rls(tx, user: UserContext) -> None:
    """Apply Postgres RLS GUCs inside a backend-neutral transaction.

    No-op on SQLite (no RLS). Postgres ``transactional()`` already
    opened a transaction before yielding, so ``SET LOCAL`` applies
    only within that scope.

    Repository SQL also bakes the visibility predicate inline as
    primary enforcement; this helper is defense-in-depth for the
    Postgres path. Every read endpoint that goes through
    ``backend.transactional()`` should call this before the first
    repository read so RLS-enabled deployments do not fall back to
    the personal_bypass policy when ``mnemos.current_user_id`` is
    unset.
    """
    if not _lc._rls_enabled or not user.authenticated:
        return
    from mnemos.persistence.postgres import PostgresTransaction

    if not isinstance(tx, PostgresTransaction):
        return
    # Postgres ``SET LOCAL`` syntax does NOT accept bind parameters
    # (https://www.postgresql.org/docs/current/sql-set.html — value
    # must be a literal). Use ``set_config(name, value, is_local)``
    # instead, which IS a function and therefore parameterizable;
    # third argument ``true`` makes it transaction-local, equivalent
    # to SET LOCAL. The earlier ``SET LOCAL ... = $1`` form would
    # raise a syntax error on RLS-enabled Postgres deployments,
    # converting every authenticated read into a 500 before the
    # protected query ran.
    await tx.conn.execute(
        "SELECT set_config('mnemos.current_user_id', $1, true)",
        user.user_id,
    )
    await tx.conn.execute(
        "SELECT set_config('mnemos.current_role', $1, true)",
        user.role,
    )


__all__ = ["backend_or_503", "maybe_set_pg_rls", "require_postgres_pool_or_503"]
