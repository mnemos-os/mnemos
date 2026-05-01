"""Tests for the ``mnemos dump-openapi`` CLI command.

The command produces the ``mnemos-openapi.json`` artifact described
in ROADMAP.md as a v4.1 connector deliverable: a static JSON copy
of the FastAPI OpenAPI spec for OpenAPI-aware clients (Custom GPTs,
OpenAI Actions bridges, Cursor HTTP MCP, ChatGPT Pro Developer Mode)
that don't want to boot the server to grab ``/openapi.json``.

These tests verify:
  * The command outputs valid JSON to stdout by default.
  * ``--output PATH`` writes to a file.
  * The output is a well-formed OpenAPI 3.x spec (paths object,
    info object, openapi version key).
  * ``--indent`` controls JSON formatting.
  * ``--title`` overrides the spec title.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from mnemos.cli.main import app


runner = CliRunner()


def test_dump_openapi_to_stdout_emits_valid_json():
    """No --output / no -o → JSON spec on stdout."""
    result = runner.invoke(app, ["dump-openapi"])
    assert result.exit_code == 0, result.output
    spec = json.loads(result.output)
    # OpenAPI 3.x has these top-level keys.
    assert "openapi" in spec
    assert "info" in spec
    assert "paths" in spec


def test_dump_openapi_writes_to_file(tmp_path: Path):
    """--output PATH writes the spec to that file and confirms via
    a stdout message."""
    out = tmp_path / "mnemos-openapi.json"
    result = runner.invoke(app, ["dump-openapi", "--output", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    spec = json.loads(out.read_text())
    assert "openapi" in spec
    assert "paths" in spec
    # Stdout should mention the output path so CI workflows can
    # confirm the artifact landed.
    assert str(out) in result.output


def test_dump_openapi_output_short_flag(tmp_path: Path):
    """``-o`` short alias works the same as ``--output``."""
    out = tmp_path / "spec.json"
    result = runner.invoke(app, ["dump-openapi", "-o", str(out)])
    assert result.exit_code == 0
    assert out.exists()


def test_dump_openapi_dash_means_stdout(tmp_path: Path):
    """``--output -`` is the explicit stdout marker (parallels the
    common Unix convention for tools like ``jq -``)."""
    result = runner.invoke(app, ["dump-openapi", "--output", "-"])
    assert result.exit_code == 0
    spec = json.loads(result.output)
    assert "openapi" in spec


def test_dump_openapi_indent_zero_emits_single_line():
    """``--indent 0`` produces a compact single-line representation
    suitable for piping into smaller targets / docker layers."""
    result = runner.invoke(app, ["dump-openapi", "--indent", "0"])
    assert result.exit_code == 0
    body = result.output.strip()
    # Single-line JSON — no embedded newlines other than the trailing one.
    assert "\n" not in body, f"expected single-line JSON; got: {body[:120]!r}"


def test_dump_openapi_indent_default_is_two_spaces():
    """Default indent is 2 spaces — readable for diffing."""
    result = runner.invoke(app, ["dump-openapi"])
    assert result.exit_code == 0
    # A pretty-printed JSON with 2-space indent should have lines
    # that begin with two spaces (e.g., "  \"openapi\": ...")
    assert '\n  "' in result.output


def test_dump_openapi_title_override(tmp_path: Path):
    """--title overrides the info.title without modifying the
    underlying app."""
    out = tmp_path / "spec.json"
    result = runner.invoke(
        app, ["dump-openapi", "--output", str(out), "--title", "MNEMOS Custom"],
    )
    assert result.exit_code == 0
    spec = json.loads(out.read_text())
    assert spec["info"]["title"] == "MNEMOS Custom"


def test_dump_openapi_indent_clamped_above_max():
    """Indent > 8 should raise (Typer's ``max`` validator), not
    silently accept."""
    result = runner.invoke(app, ["dump-openapi", "--indent", "99"])
    assert result.exit_code != 0
