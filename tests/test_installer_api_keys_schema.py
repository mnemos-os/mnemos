"""Installer api_keys INSERT must match the v1_multiuser schema.

Regression for #M31-04. The installer previously wrote columns
(`name`, `permissions`) that no longer exist on `api_keys`; the current
schema has (`user_id`, `key_prefix`, `label`). Auth-enabled fresh
installs failed at seed with "column does not exist".

This test is a static SQL-text contract check — it reads the installer
source and the migration source, extracts the column list from each,
and asserts the installer's INSERT targets only columns that exist.
No running database required.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
INSTALLER_DB = REPO_ROOT / "mnemos" / "installer" / "db.py"
MIGRATION = REPO_ROOT / "db" / "migrations_v1_multiuser.sql"


def _api_keys_columns_in_schema() -> set[str]:
    """Pull api_keys columns from the CREATE TABLE definition in the
    migration SQL."""
    sql = MIGRATION.read_text(encoding="utf-8")
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS api_keys\s*\((.*?)\);",
        sql,
        flags=re.DOTALL,
    )
    assert match, "api_keys table not found in migrations_v1_multiuser.sql"
    body = match.group(1)
    # Column lines look like:  `column_name   TYPE modifiers,`
    cols: set[str] = set()
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        # First token is the column name.
        token = line.split()[0].strip('"')
        # Skip constraint lines ("PRIMARY KEY", "CHECK", "FOREIGN KEY").
        if token.upper() in {"PRIMARY", "CHECK", "FOREIGN", "UNIQUE", "CONSTRAINT"}:
            continue
        cols.add(token.lower())
    return cols


def _installer_inserts_columns() -> list[set[str]]:
    """Pull the column lists out of every INSERT INTO api_keys (...) in
    mnemos/installer/db.py — the file has three code paths (psycopg, psycopg2,
    psql-CLI fallback). All must match the schema."""
    src = INSTALLER_DB.read_text(encoding="utf-8")
    matches = re.findall(
        r"INSERT INTO api_keys\s*\(([^)]+)\)",
        src,
        flags=re.IGNORECASE,
    )
    assert matches, "mnemos/installer/db.py has no INSERT INTO api_keys — extraction broken"
    return [
        {col.strip().strip('"').lower() for col in m.split(",") if col.strip()}
        for m in matches
    ]


def test_installer_insert_columns_exist_in_schema():
    schema_cols = _api_keys_columns_in_schema()
    assert "user_id" in schema_cols, (
        "api_keys schema lacks user_id — did the migration change?"
    )
    assert "key_hash" in schema_cols
    assert "key_prefix" in schema_cols
    assert "label" in schema_cols

    for i, installer_cols in enumerate(_installer_inserts_columns()):
        unknown = installer_cols - schema_cols
        assert not unknown, (
            f"mnemos/installer/db.py INSERT #{i+1} targets columns that do not exist on "
            f"api_keys: {sorted(unknown)}. The schema columns are: "
            f"{sorted(schema_cols)}. This is #M31-04 — auth-enabled installs "
            f"will fail with 'column does not exist' at seed."
        )


def test_installer_insert_supplies_required_nonnull_columns():
    """user_id, key_hash, key_prefix are NOT NULL in the schema; every
    INSERT must supply them (label and the timestamp defaults are optional)."""
    required = {"user_id", "key_hash", "key_prefix"}
    for i, installer_cols in enumerate(_installer_inserts_columns()):
        missing = required - installer_cols
        assert not missing, (
            f"mnemos/installer/db.py INSERT #{i+1} is missing required NOT NULL "
            f"columns: {sorted(missing)}. Present: {sorted(installer_cols)}."
        )


def test_installer_seeds_user_before_api_key():
    """The api_keys.user_id FK references users(id). The v1 multiuser
    migration seeds a 'default' user (lines 60-62); the installer's
    INSERT must reference that user, not create dangling rows."""
    src = INSTALLER_DB.read_text(encoding="utf-8")
    # Look for the parameterized default-user reference in psycopg calls
    # OR the literal 'default' in the psql-CLI fallback.
    has_default_ref = (
        "'default'" in src
        or '"default"' in src
        or re.search(r'\(\s*"default"', src)
    )
    assert has_default_ref, (
        "mnemos/installer/db.py never references the 'default' user when inserting "
        "api_keys — api_keys.user_id FK will fail. The migration seeds "
        "users(id='default'); the installer must reuse it."
    )
