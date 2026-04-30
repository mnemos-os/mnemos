from __future__ import annotations

import pytest
from typer.testing import CliRunner

from mnemos._version import __version__
from mnemos.cli import main as cli_main
from mnemos.core import config as core_config


runner = CliRunner()


def test_top_level_help_lists_all_subcommands() -> None:
    result = runner.invoke(cli_main.app, ["--help"])

    assert result.exit_code == 0
    for command in (
        "serve",
        "worker",
        "install",
        "export",
        "import",
        "validate-mpf",
        "consult",
        "health",
        "version",
    ):
        assert command in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["serve", "--help"],
        ["serve", "mcp-stdio", "--help"],
        ["serve", "mcp-http", "--help"],
        ["worker", "--help"],
        ["worker", "distillation", "--help"],
        ["install", "--help"],
        ["export", "--help"],
        ["import", "--help"],
        ["validate-mpf", "--help"],
        ["consult", "--help"],
        ["health", "--help"],
        ["version", "--help"],
    ],
)
def test_subcommand_help_works(args: list[str]) -> None:
    result = runner.invoke(cli_main.app, args)

    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_version_prints_package_version() -> None:
    result = runner.invoke(cli_main.app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_dispatch_table_is_complete() -> None:
    assert cli_main.EXPORT_DISPATCH == {
        "mpf": "mnemos.tools.memory_export",
        "jsonl": "mnemos.tools.memory_export",
        "markdown": "mnemos.tools.export_memories_for_docling",
        "html": "mnemos.tools.export_memories_for_docling",
        "text": "mnemos.tools.export_memories_for_docling",
    }
    assert cli_main.IMPORT_DISPATCH == {
        "mpf": "mnemos.tools.memory_import",
        "docling": "mnemos.tools.docling_import",
        "mem0": "mnemos.tools.adapters.mem0",
        "letta": "mnemos.tools.adapters.letta",
        "graphiti": "mnemos.tools.adapters.graphiti",
        "cognee": "mnemos.tools.adapters.cognee",
        "mempalace": "mnemos.tools.adapters.mempalace",
    }


def test_validate_mpf_dispatches_to_validator(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_run_module_main(module: str, argv: list[str], **_kwargs) -> None:
        calls.append((module, argv))

    monkeypatch.setattr(cli_main, "_run_module_main", fake_run_module_main)

    result = runner.invoke(cli_main.app, ["validate-mpf", "memory.mpf"])

    assert result.exit_code == 0
    assert calls == [("mnemos.tools.mpf_validate", ["--file", "memory.mpf"])]


def test_export_dispatches_to_memory_export(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls = []

    def fake_run_module_main(module: str, argv: list[str], **_kwargs) -> None:
        calls.append((module, argv))

    monkeypatch.delenv("MNEMOS_BASE", raising=False)
    monkeypatch.delenv("MNEMOS_API_KEY", raising=False)
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "missing.toml"))
    core_config.reload_settings()
    monkeypatch.setattr(cli_main, "_run_module_main", fake_run_module_main)

    result = runner.invoke(
        cli_main.app,
        ["export", "--format", "jsonl", "--out", "memories.jsonl", "--owner-id", "alice", "--namespace", "lab"],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "mnemos.tools.memory_export",
            ["jsonl", "--out", "memories.jsonl", "--owner-id", "alice", "--namespace", "lab"],
        )
    ]


def test_mpf_import_dispatches_to_memory_import(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls = []

    def fake_run_module_main(module: str, argv: list[str], **_kwargs) -> None:
        calls.append((module, argv))

    monkeypatch.delenv("MNEMOS_BASE", raising=False)
    monkeypatch.delenv("MNEMOS_API_KEY", raising=False)
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "missing.toml"))
    core_config.reload_settings()
    monkeypatch.setattr(cli_main, "_run_module_main", fake_run_module_main)

    result = runner.invoke(
        cli_main.app,
        ["import", "memories.json", "--from", "mpf", "--preserve-owner", "--namespace", "lab"],
    )

    assert result.exit_code == 0
    assert calls == [
        (
            "mnemos.tools.memory_import",
            ["json", "--file", "memories.json", "--preserve-metadata", "--namespace", "lab"],
        )
    ]
