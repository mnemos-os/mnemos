"""SQL-shape regression for mnemos_version_snapshot branch-head locking."""

from __future__ import annotations

import re
from pathlib import Path


def _trigger_sql() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return (repo_root / "db" / "migrations_v3_5_trigger_same_memory_parent.sql").read_text()


def _compact(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _extract_update_branch(sql: str) -> str:
    try:
        return sql.split("ELSIF TG_OP = 'UPDATE' THEN", 1)[1].split(
            "ELSIF TG_OP = 'DELETE' THEN",
            1,
        )[0]
    except IndexError as exc:
        raise AssertionError("could not isolate mnemos_version_snapshot UPDATE branch") from exc


def _extract_delete_branch(sql: str) -> str:
    try:
        return sql.split("ELSIF TG_OP = 'DELETE' THEN", 1)[1].split(
            "\n    IF TG_OP = 'DELETE' THEN",
            1,
        )[0]
    except IndexError as exc:
        raise AssertionError("could not isolate mnemos_version_snapshot DELETE branch") from exc


def _assert_locked_parent_resolution(branch_sql: str, row_id: str) -> None:
    compact = _compact(branch_sql)
    insert_pos = compact.index("INSERT INTO memory_versions")

    locked_parent_select = re.compile(
        rf"SELECT mb\.head_version_id INTO _parent_version "
        rf"FROM memory_branches mb "
        rf"INNER JOIN memory_versions mv "
        rf"ON mv\.id = mb\.head_version_id "
        rf"AND mv\.memory_id = mb\.memory_id "
        rf"WHERE mb\.memory_id = {re.escape(row_id)} "
        rf"AND mb\.name = _branch "
        rf"FOR UPDATE OF mb"
    )
    match = locked_parent_select.search(compact)

    assert match is not None
    assert match.start() < insert_pos


def _assert_post_update_row_count_guard(branch_sql: str, row_id: str) -> None:
    compact = _compact(branch_sql)
    advance_head = (
        "UPDATE memory_branches SET head_version_id = _new_version_id "
        f"WHERE memory_id = {row_id} AND name = _branch"
    )

    update_pos = compact.index(advance_head)
    diagnostics_pos = compact.index("GET DIAGNOSTICS _updated_rows = ROW_COUNT", update_pos)
    guard_pos = compact.index("IF _updated_rows = 0 THEN RAISE EXCEPTION", diagnostics_pos)
    errcode_pos = compact.index("USING ERRCODE = 'MN001'", guard_pos)

    assert "disappeared before head update" in compact[guard_pos:errcode_pos]


def test_update_and_delete_trigger_arms_lock_branch_head_resolution():
    sql = _trigger_sql()

    for branch_sql, row_id in (
        (_extract_update_branch(sql), "NEW.id"),
        (_extract_delete_branch(sql), "OLD.id"),
    ):
        _assert_locked_parent_resolution(branch_sql, row_id)


def test_update_and_delete_trigger_arms_guard_missing_branch_head_advance():
    sql = _trigger_sql()

    for branch_sql, row_id in (
        (_extract_update_branch(sql), "NEW.id"),
        (_extract_delete_branch(sql), "OLD.id"),
    ):
        _assert_post_update_row_count_guard(branch_sql, row_id)
