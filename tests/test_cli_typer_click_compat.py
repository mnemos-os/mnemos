from __future__ import annotations

import importlib
from typing import Any

import pytest
import typer
from click.testing import CliRunner
from typer.core import TyperOption
from typer.main import get_command

from mnemos._version import __version__


def test_import_installs_typer_click_compat_shim() -> None:
    cli_main = importlib.import_module("mnemos.cli.main")

    assert getattr(TyperOption.__init__, "_mnemos_click_compat", False)
    assert cli_main.app is not None


def test_typer_flag_option_parses_after_compat_shim() -> None:
    importlib.import_module("mnemos.cli.main")
    app = typer.Typer()
    parsed: dict[str, bool] = {}

    @app.command()
    def command(flag: bool = typer.Option(False, "--flag", is_flag=True)) -> None:
        parsed["flag"] = flag

    result = CliRunner().invoke(get_command(app), ["--flag"])

    assert result.exit_code == 0, result.output
    assert parsed == {"flag": True}


def test_typer_option_shim_drops_unsupported_flag_value(monkeypatch: pytest.MonkeyPatch) -> None:
    cli_main = importlib.import_module("mnemos.cli.main")
    calls: list[dict[str, Any]] = []

    def fake_option_init(
        self: object,
        *,
        param_decls: list[str],
        type: Any = "sentinel",
        is_flag: bool | None = None,
    ) -> None:
        calls.append({"param_decls": param_decls, "type": type, "is_flag": is_flag})

    monkeypatch.setattr(TyperOption, "__init__", fake_option_init)

    cli_main._patch_typer_click_compat()
    TyperOption(param_decls=["--flag"], is_flag=True, flag_value=None, unsupported="dropped")

    assert calls == [{"param_decls": ["--flag"], "type": None, "is_flag": True}]


def test_mnemos_version_subcommand_smoke_does_not_touch_network(monkeypatch: pytest.MonkeyPatch) -> None:
    cli_main = importlib.import_module("mnemos.cli.main")

    def fail_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("version smoke must not call the network")

    monkeypatch.setattr(cli_main.httpx, "get", fail_network)
    monkeypatch.setattr(cli_main.httpx, "post", fail_network)

    result = CliRunner().invoke(get_command(cli_main.app), ["version"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == __version__
    assert "4.2.0a3" in result.output
