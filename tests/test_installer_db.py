"""Installer database helper regressions."""

from __future__ import annotations

from mnemos.installer import db


def test_psql_superuser_file_streams_sql_via_stdin(monkeypatch, tmp_path):
    sql = "SELECT 'migration via stdin';\n"
    migration = tmp_path / "_MEIfake" / "migrations.sql"
    migration.parent.mkdir()
    migration.write_text(sql, encoding="utf-8")

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return 0, "", ""

    monkeypatch.setattr(db, "_run", fake_run)

    db._psql_superuser_file(str(migration), "mnemos")

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[args.index("-f") + 1] == "-"
    assert str(migration) not in args
    assert kwargs["input"] == sql
