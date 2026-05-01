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
    await tx.conn.execute("SET LOCAL mnemos.current_user_id = $1", user.user_id)
    await tx.conn.execute("SET LOCAL mnemos.current_role = $1", user.role)


__all__ = ["backend_or_503", "maybe_set_pg_rls"]
