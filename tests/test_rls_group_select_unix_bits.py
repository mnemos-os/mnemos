"""Regression pins for the v3.5 RLS group-select policy fix."""
from __future__ import annotations

import re
from pathlib import Path


MIGRATION_NAME = "migrations_v3_5_rls_group_select_unix_bits.sql"


def _migration_sql() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return (repo_root / "db" / MIGRATION_NAME).read_text()


def test_rls_group_select_policy_uses_unix_group_bits():
    sql = _migration_sql()
    compact = " ".join(sql.split())

    assert "CREATE POLICY mnemos_group_select ON memories" in compact
    assert "FOR SELECT TO mnemos_user" in compact
    assert re.search(r"\(\(\s*permission_mode\s*/\s*10\s*\)\s*%\s*10\s*\)\s*>=\s*4", sql)
    assert not re.search(r"permission_mode\s*>=\s*640", sql)


def test_rls_group_select_policy_replaces_existing_policy_first():
    sql = _migration_sql()

    drop_idx = sql.index("DROP POLICY mnemos_group_select ON memories;")
    create_idx = sql.index("CREATE POLICY mnemos_group_select ON memories")

    assert drop_idx < create_idx


def test_rls_group_select_policy_keeps_group_membership_gate():
    sql = _migration_sql()

    assert re.search(
        r"USING\s*\(.*"
        r"group_id\s+IS\s+NOT\s+NULL.*"
        r"EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+user_groups.*"
        r"user_id::text\s*=\s*current_setting\('mnemos\.current_user_id',\s*TRUE\).*"
        r"group_id\s*=\s*memories\.group_id",
        sql,
        re.DOTALL,
    )
