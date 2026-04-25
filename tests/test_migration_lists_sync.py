"""Regression guard: install.py and installer/db.py migration lists
must stay in sync. Codex review (thread 019dbcc5) flagged drift
between the two as a fresh-install footgun — operators following
the README ran install.py, which stopped at v3_ownership and left
the v3.1.x tables missing, producing 503 on /v1/consultations.

This test extracts the `migration_files` list from each file via
AST and asserts they're equal, in order. It's intentionally a
mechanical check — if you need to add a migration, add it to BOTH
lists (at the end) and this test will pass again.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _extract_migration_list(source_path: Path, func_name: str) -> list[str]:
    """Parse the .py file, find `def <func_name>`, return the list of
    basenames assigned to `migration_files` inside that function.

    Both install.py and installer/db.py build the list with
    `os.path.join(..., "db", "<file>.sql")` or `repo_path / "db" / "<file>.sql"`.
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


def test_install_py_and_installer_db_lists_are_identical():
    repo_root = Path(__file__).resolve().parents[1]
    install_py_list = _extract_migration_list(repo_root / "install.py", "main")
    installer_db_list = _extract_migration_list(repo_root / "installer" / "db.py", "run_migrations")

    assert install_py_list == installer_db_list, (
        "install.py and installer/db.py migration lists have drifted.\n"
        f"  install.py ({len(install_py_list)} entries):      {install_py_list}\n"
        f"  installer/db.py ({len(installer_db_list)} entries): {installer_db_list}\n"
        "Both lists must be identical and in the same order. When adding "
        "a new migration, append to the END of BOTH lists."
    )


def test_every_migration_list_entry_exists_on_disk():
    """Catches the other common mistake: adding a migration to one
    of the lists without the corresponding SQL file actually existing
    in db/. A fresh install would skip silently per installer/db.py:243
    (warn + continue) — this test makes the omission a CI failure."""
    repo_root = Path(__file__).resolve().parents[1]
    install_py_list = _extract_migration_list(repo_root / "install.py", "main")

    missing = []
    for name in install_py_list:
        if not (repo_root / "db" / name).exists():
            missing.append(name)
    assert not missing, (
        f"Migration entries reference files that don't exist in db/: {missing}. "
        "Either remove the entry or add the SQL file."
    )


def _extract_docker_compose_migrations(compose_path: Path) -> list[str]:
    """Pull migration filenames from docker-compose.yml's volume
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
        out.append(Path(host_path).name)
    return out


def test_docker_compose_migration_list_matches_installer():
    """Codex round-26 finding: docker-compose.yml maintained its
    own migration init list and drifted behind installer/db.py
    (stopped at v3_1_versioning_fix while CHARON v0.2 added 9
    more migrations through migrations_charon_trigger_guard.sql).
    Fresh `docker compose up` databases would have kg_triples
    without owner_id/namespace and no trigger guard, breaking
    /v1/export?include_sidecars=true.

    The Docker init list must be a (possibly proper) prefix of
    the installer/db.py list — never strictly less if a newer
    migration is required by shipped code paths. We assert exact
    equality so a future drift is caught immediately."""
    repo_root = Path(__file__).resolve().parents[1]
    installer_list = _extract_migration_list(
        repo_root / "installer" / "db.py", "run_migrations",
    )
    compose_list = _extract_docker_compose_migrations(
        repo_root / "docker-compose.yml",
    )
    assert installer_list == compose_list, (
        "docker-compose.yml migration list has drifted from "
        "installer/db.py.\n"
        f"  installer/db.py ({len(installer_list)} entries):    "
        f"{installer_list}\n"
        f"  docker-compose.yml ({len(compose_list)} entries): "
        f"{compose_list}\n"
        "When adding a new migration, append to ALL THREE: "
        "install.py, installer/db.py, AND docker-compose.yml."
    )
