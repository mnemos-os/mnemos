"""Helpers for endpoints that still depend on PostgreSQL SQL surfaces."""
from __future__ import annotations

from fastapi import HTTPException

import mnemos.core.lifecycle as _lc


def _backend_or_503():
    backend = _lc._persistence_backend
    if backend is None:
        raise HTTPException(status_code=503, detail="Persistence backend not available")
    return backend


def _require_postgres_backend():
    """Return the active Postgres backend or emit the edge-profile 503."""
    from mnemos.persistence.postgres import PostgresBackend

    backend = _backend_or_503()
    if not isinstance(backend, PostgresBackend):
        raise HTTPException(
            status_code=503,
            detail="this endpoint requires a Postgres backend (server profile)",
        )
    return backend

