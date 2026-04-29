"""Shared authorization helpers for API handlers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException

from mnemos.core.auth_context import UserContext

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CAST_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\[\])?$")

_NOT_FOUND_DETAILS = {
    "entities": "Entity not found",
    "memories": "Memory not found",
    "sessions": "Session not found",
    "kg_triples": "Triple not found",
    "graeae_consultations": "Consultation not found",
}


@dataclass(frozen=True)
class TenancyContext:
    user: UserContext
    owner: str
    namespace: str

    def __iter__(self):
        yield self.owner
        yield self.namespace


def is_root(user: UserContext) -> bool:
    return user.role == "root"


def scope_owner(user: UserContext, override: Optional[str]) -> str:
    if override and override != user.user_id:
        if not is_root(user):
            raise HTTPException(status_code=403, detail="owner_id override requires root")
        return override
    return user.user_id


def scope_namespace(user: UserContext, override: Optional[str]) -> str:
    if override and override != user.namespace:
        if not is_root(user):
            raise HTTPException(
                status_code=403,
                detail="cross-namespace access requires root",
            )
        return override
    return user.namespace


async def assert_owned(
    conn,
    table: str,
    resource_id: str,
    user: UserContext,
    *,
    id_column: str = "id",
    id_cast: str = "uuid",
) -> str:
    context = await assert_owned_context(
        conn,
        table,
        resource_id,
        user,
        id_column=id_column,
        id_cast=id_cast,
    )
    return context.owner


async def assert_owned_context(
    conn,
    table: str,
    resource_id: str,
    user: UserContext,
    *,
    id_column: str = "id",
    id_cast: str = "uuid",
) -> TenancyContext:
    safe_table = _sql_identifier(table, "table")
    safe_id_column = _sql_identifier(id_column, "id_column")
    id_expr = f"$1::{_sql_cast(id_cast)}" if id_cast else "$1"
    row = await conn.fetchrow(
        f"SELECT owner_id, namespace FROM {safe_table} WHERE {safe_id_column} = {id_expr}",
        resource_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail=_not_found_detail(table))
    if not is_root(user) and (
        row["owner_id"] != user.user_id
        or row["namespace"] != user.namespace
    ):
        raise HTTPException(status_code=404, detail=_not_found_detail(table))
    return TenancyContext(
        user=user,
        owner=row["owner_id"],
        namespace=row["namespace"],
    )


def assert_owner_match(resource_owner_id: str, user: UserContext) -> None:
    if not is_root(user) and resource_owner_id != user.user_id:
        raise HTTPException(status_code=403, detail="owner_id mismatch requires root")


def _not_found_detail(table: str) -> str:
    return _NOT_FOUND_DETAILS.get(table, "Resource not found")


def _sql_identifier(value: str, label: str) -> str:
    parts = value.split(".")
    if not parts or any(not _IDENTIFIER_RE.match(part) for part in parts):
        raise ValueError(f"Unsafe SQL {label}: {value!r}")
    return ".".join(parts)


def _sql_cast(value: str) -> str:
    if not _CAST_RE.match(value):
        raise ValueError(f"Unsafe SQL id_cast: {value!r}")
    return value
