"""Regression guard for the canonical installer migration list.

The v4 package layout removes the root install.py wrapper, so
mnemos/installer/db.py is the single source of truth. These tests
extract its `migration_files` list via AST and ensure compose files
stay in sync.
"""
from __future__ import annotations

import ast
from pathlib import Path

EXPECTED_MIGRATIONS = [
    "migrations.sql",
    "migrations_v1_multiuser.sql",
    "migrations_v2_versioning.sql",
    "migrations_v2_sessions.sql",
    "migrations_model_registry.sql",
    "migrations_v3_dag.sql",
    "migrations_v3_graeae_unified.sql",
    "migrations_v3_webhooks.sql",
    "migrations_v3_oauth.sql",
    "migrations_v3_federation.sql",
    "migrations_v3_ownership.sql",
    "migrations_v3_1_compression.sql",
    "migrations_v3_1_versioning_fix.sql",
    "migrations_v3_1_2_kg_tenancy.sql",
    "migrations_v3_1_2_audit_log_columns.sql",
    "migrations_v3_2_user_namespace.sql",
    "migrations_v3_2_entities_namespace.sql",
    "migrations_v3_2_2_version_snapshot_new_values.sql",
    "migrations_v3_3_morpheus.sql",
    "migrations_v3_3_morpheus_namespace.sql",
    "migrations_v3_3_recall_tracking.sql",
    "migrations_charon_trigger_guard.sql",
    "migrations_v3_4_federation_compat.sql",
    "migrations_v3_5_trigger_same_memory_parent.sql",
    "migrations_v3_5_rls_group_select_unix_bits.sql",
    "migrations_v3_5_webhook_retry_terminal_state.sql",
    "migrations_v3_5_webhook_attempt_lease.sql",
    "migrations_v3_5_webhook_writer_revision.sql",
    "migrations_v3_5_webhook_status_updated_at.sql",
    "migrations_v3_5_webhook_superseded_marker.sql",
    "migrations_v3_5_webhook_attempt_unique.sql",
    "migrations_v3_5_webhook_succeeded_unique.sql",
    "migrations_v3_5_webhook_succeeded_terminal_trigger.sql",
    "migrations_v3_5_entities_namespace_unique.sql",
    "migrations_v3_5_state_journal_namespace.sql",
    "migrations_v3_5_session_compression_ratio_drop.sql",
    "migrations_v3_5_session_compression_legacy_drop.sql",
    "migrations_v3_5_sessions_consultations_namespace.sql",
    "migrations_v4_2_users_username.sql",
    "migrations_v4_2_compression_candidates_nullable_tokens.sql",
    "migrations_v4_2_state_value_text.sql",
    "migrations_v4_2_document_import_chunk_idempotency.sql",
    "migrations_v4_2_deletion_requests.sql",
    "migrations_v4_2_deletion_requests_blank_namespace_cleanup.sql",
    "migrations_v4_2_deletion_requests_soft_delete_columns.sql",
    "migrations_v4_2_deletion_requests_sweep_verifying.sql",
    "migrations_v4_2_compression_dag.sql",
]


def _extract_migration_list(source_path: Path, func_name: str) -> list[str]:
    """Parse the .py file, find `def <func_name>`, return the list of
    basenames assigned to `migration_files` inside that function.

    The installer builds the list with `repo_path / "db" / "<file>.sql"`.
    We walk the AST, find the `migration_files = [...]` assignment,
    and collect the final string literal from each element.
    """
    tree = ast.parse(source_path.read_text())

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            for stmt in ast.walk(node):
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == "migration_files"
                    and isinstance(stmt.value, ast.List)
                ):
                    names: list[str] = []
                    for elt in stmt.value.elts:
                        # Walk backwards through the call/binop to find
                        # the last string constant (the .sql filename).
                        last_str: str | None = None
                        for sub in ast.walk(elt):
                            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                                if sub.value.endswith(".sql"):
                                    last_str = sub.value
                        if last_str is None:
                            raise AssertionError(
                                f"could not find .sql filename in list element: {ast.dump(elt)}"
                            )
                        names.append(last_str)
                    return names
    raise AssertionError(f"no migration_files list found in {source_path}::{func_name}")


def test_installer_db_migration_list_matches_expected_order():
    repo_root = Path(__file__).resolve().parents[1]
    installer_db_list = _extract_migration_list(repo_root / "mnemos" / "installer" / "db.py", "run_migrations")

    assert installer_db_list == EXPECTED_MIGRATIONS


def test_every_migration_list_entry_exists_on_disk():
    """Catches the other common mistake: adding a migration to one
    of the lists without the corresponding SQL file actually existing
    in db/. A fresh install would skip silently per mnemos/installer/db.py:243
    (warn + continue) — this test makes the omission a CI failure."""
    repo_root = Path(__file__).resolve().parents[1]
    installer_db_list = _extract_migration_list(repo_root / "mnemos" / "installer" / "db.py", "run_migrations")

    missing = []
    for name in installer_db_list:
        if not (repo_root / "db" / name).exists():
            missing.append(name)
    assert not missing, (
        f"Migration entries reference files that don't exist in db/: {missing}. "
        "Either remove the entry or add the SQL file."
    )


def _extract_docker_compose_migrations(compose_path: Path) -> list[str]:
    """Pull migration filenames from docker-compose volume
    mounts. Each mount looks like:
      - ./db/migrations_*.sql:/docker-entrypoint-initdb.d/NN-name.sql
    """
    text = compose_path.read_text()
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("- ./db/"):
            continue
        if "/docker-entrypoint-initdb.d/" not in line:
            continue
        # `- ./db/<file>.sql:/docker-entrypoint-initdb.d/...`
        host_path = line.split(":", 1)[0]  # `- ./db/<file>.sql`
        host_path = host_path.removeprefix("- ").strip()
        name = Path(host_path).name
        if not name.startswith("migrations"):
            continue
        out.append(name)
    return out


def test_docker_compose_migration_lists_match_installer():
    """Codex round-26 finding: docker-compose*.yml maintained their
    own migration init lists and drifted behind mnemos/installer/db.py
    (stopped at v3_1_versioning_fix while CHARON v0.2 added 9
    more migrations through migrations_charon_trigger_guard.sql).
    Fresh `docker compose up` databases would have kg_triples
    without owner_id/namespace and no trigger guard, breaking
    /v1/export?include_sidecars=true.

    The Docker init list must be a (possibly proper) prefix of
    the mnemos/installer/db.py list — never strictly less if a newer
    migration is required by shipped code paths. We assert exact
    equality so a future drift is caught immediately."""
    repo_root = Path(__file__).resolve().parents[1]
    installer_list = _extract_migration_list(
        repo_root / "mnemos" / "installer" / "db.py", "run_migrations",
    )
    for compose_name in ("docker-compose.yml", "docker-compose.staging.yml"):
        compose_list = _extract_docker_compose_migrations(repo_root / compose_name)
        assert installer_list == compose_list, (
            f"{compose_name} migration list has drifted from mnemos/installer/db.py.\n"
            f"  mnemos/installer/db.py ({len(installer_list)} entries): "
            f"{installer_list}\n"
            f"  {compose_name} ({len(compose_list)} entries): "
            f"{compose_list}\n"
            "When adding a new migration, append to ALL THREE: "
            "mnemos/installer/db.py, docker-compose.yml, "
            "AND docker-compose.staging.yml."
        )


def test_compose_files_run_v3_5_upgrades_for_existing_volumes():
    """The initdb mount only affects fresh Postgres volumes.

    Dev and PROTEUS staging keep named postgres_data volumes, so the
    v3.5 database patches also need an explicit compose-time upgrade
    service that runs against already-initialized databases.
    """
    repo_root = Path(__file__).resolve().parents[1]
    for compose_name in ("docker-compose.yml", "docker-compose.staging.yml"):
        text = (repo_root / compose_name).read_text()

        assert "postgres-upgrade:" in text, compose_name
        assert (
            "./db/migrations_v3_5_rls_group_select_unix_bits.sql:"
            "/docker-entrypoint-initdb.d/25-rls-group-select-unix-bits.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_retry_terminal_state.sql:"
            "/docker-entrypoint-initdb.d/26-webhook-retry-terminal-state.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_attempt_lease.sql:"
            "/docker-entrypoint-initdb.d/27-webhook-attempt-lease.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_writer_revision.sql:"
            "/docker-entrypoint-initdb.d/28-webhook-writer-revision.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_status_updated_at.sql:"
            "/docker-entrypoint-initdb.d/29-webhook-status-updated-at.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_superseded_marker.sql:"
            "/docker-entrypoint-initdb.d/30-webhook-superseded-marker.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_attempt_unique.sql:"
            "/docker-entrypoint-initdb.d/31-webhook-attempt-unique.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_succeeded_unique.sql:"
            "/docker-entrypoint-initdb.d/32-webhook-succeeded-unique.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_succeeded_terminal_trigger.sql:"
            "/docker-entrypoint-initdb.d/33-webhook-succeeded-terminal-trigger.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_entities_namespace_unique.sql:"
            "/docker-entrypoint-initdb.d/34-entities-namespace-unique.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_state_journal_namespace.sql:"
            "/docker-entrypoint-initdb.d/35-state-journal-namespace.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_session_compression_ratio_drop.sql:"
            "/docker-entrypoint-initdb.d/36-session-compression-ratio-drop.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_session_compression_legacy_drop.sql:"
            "/docker-entrypoint-initdb.d/37-session-compression-legacy-drop.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_sessions_consultations_namespace.sql:"
            "/docker-entrypoint-initdb.d/38-sessions-consultations-namespace.sql"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_trigger_same_memory_parent.sql:"
            "/migrations/24-trigger-same-memory-parent.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_rls_group_select_unix_bits.sql:"
            "/migrations/25-rls-group-select-unix-bits.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_retry_terminal_state.sql:"
            "/migrations/26-webhook-retry-terminal-state.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_attempt_lease.sql:"
            "/migrations/27-webhook-attempt-lease.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_writer_revision.sql:"
            "/migrations/28-webhook-writer-revision.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_status_updated_at.sql:"
            "/migrations/29-webhook-status-updated-at.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_superseded_marker.sql:"
            "/migrations/30-webhook-superseded-marker.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_attempt_unique.sql:"
            "/migrations/31-webhook-attempt-unique.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_succeeded_unique.sql:"
            "/migrations/32-webhook-succeeded-unique.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_webhook_succeeded_terminal_trigger.sql:"
            "/migrations/33-webhook-succeeded-terminal-trigger.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_entities_namespace_unique.sql:"
            "/migrations/34-entities-namespace-unique.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_state_journal_namespace.sql:"
            "/migrations/35-state-journal-namespace.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_session_compression_ratio_drop.sql:"
            "/migrations/36-session-compression-ratio-drop.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_session_compression_legacy_drop.sql:"
            "/migrations/37-session-compression-legacy-drop.sql:ro"
        ) in text, compose_name
        assert (
            "./db/migrations_v3_5_sessions_consultations_namespace.sql:"
            "/migrations/38-sessions-consultations-namespace.sql:ro"
        ) in text, compose_name
        assert "psql -h postgres -U mnemos_user -d mnemos" in text, compose_name
        assert "-v ON_ERROR_STOP=1" in text, compose_name
        assert "-f /migrations/24-trigger-same-memory-parent.sql" in text, compose_name
        assert "-f /migrations/25-rls-group-select-unix-bits.sql" in text, compose_name
        assert "-f /migrations/26-webhook-retry-terminal-state.sql" in text, compose_name
        assert "-f /migrations/27-webhook-attempt-lease.sql" in text, compose_name
        assert "-f /migrations/28-webhook-writer-revision.sql" in text, compose_name
        assert "-f /migrations/29-webhook-status-updated-at.sql" in text, compose_name
        assert "-f /migrations/30-webhook-superseded-marker.sql" in text, compose_name
        assert "-f /migrations/31-webhook-attempt-unique.sql" in text, compose_name
        assert "-f /migrations/32-webhook-succeeded-unique.sql" in text, compose_name
        assert "-f /migrations/33-webhook-succeeded-terminal-trigger.sql" in text, compose_name
        assert "-f /migrations/34-entities-namespace-unique.sql" in text, compose_name
        assert "-f /migrations/35-state-journal-namespace.sql" in text, compose_name
        assert "-f /migrations/36-session-compression-ratio-drop.sql" in text, compose_name
        assert "-f /migrations/37-session-compression-legacy-drop.sql" in text, compose_name
        assert "-f /migrations/38-sessions-consultations-namespace.sql" in text, compose_name
        assert "postgres-upgrade:\n        condition: service_completed_successfully" in text, compose_name


def _extract_trigger_delete_branch(sql: str) -> str:
    try:
        return sql.split("ELSIF TG_OP = 'DELETE' THEN", 1)[1].split(
            "\n    IF TG_OP = 'DELETE' THEN",
            1,
        )[0]
    except IndexError as exc:
        raise AssertionError("could not isolate mnemos_version_snapshot DELETE branch") from exc


def test_v3_5_trigger_delete_branch_advances_head_to_delete_snapshot():
    """trg_memory_version_delete is live on this branch.

    The DELETE branch must capture the inserted tombstone id and move
    memory_branches.head_version_id to it, or /log and reconciliation
    callers stay pinned to the pre-delete version.
    """
    repo_root = Path(__file__).resolve().parents[1]
    sql = (repo_root / "db" / "migrations_v3_5_trigger_same_memory_parent.sql").read_text()
    delete_branch = _extract_trigger_delete_branch(sql)
    compact = " ".join(delete_branch.split())

    assert "dead code" not in sql.lower()
    assert "AND mv.memory_id = mb.memory_id" in compact
    assert (
        "_by, 'delete', _commit_hash, _branch, _parent_version ) "
        "RETURNING id INTO _new_version_id"
    ) in compact
    assert (
        "UPDATE memory_branches SET head_version_id = _new_version_id "
        "WHERE memory_id = OLD.id AND name = _branch"
    ) in compact
