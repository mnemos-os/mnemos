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


# ── --target gpt-actions: OpenAI Custom GPT field-length limits ───────────
#
# Codex round-1 of round-36 caught that the raw FastAPI spec emits
# endpoint descriptions over OpenAI's 300-char limit (one was 884
# chars), so a Custom GPT importing the artifact would fail or
# silently truncate. ``--target gpt-actions`` runs the spec through
# truncate_for_gpt_actions before serialization.


def test_full_target_keeps_long_descriptions(tmp_path: Path):
    """Confirm the regression that motivated --target gpt-actions:
    at least one endpoint description in the raw FastAPI spec is
    longer than the GPT-Actions 300-char limit."""
    out = tmp_path / "spec.json"
    result = runner.invoke(
        app, ["dump-openapi", "--target", "full", "-o", str(out)],
    )
    assert result.exit_code == 0
    spec = json.loads(out.read_text())

    long_descriptions = []
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in {
                "get", "put", "post", "delete", "options",
                "head", "patch", "trace",
            }:
                continue
            desc = op.get("description") or ""
            if len(desc) > 300:
                long_descriptions.append((method, path, len(desc)))
    assert long_descriptions, (
        "expected at least one description over 300 chars in --target full; "
        "if this fails, the full target's descriptions all happen to fit "
        "and the gpt-actions transform is no longer load-bearing"
    )


def test_gpt_actions_target_truncates_endpoint_descriptions(tmp_path: Path):
    """Pin the contract: with --target gpt-actions, NO endpoint
    description exceeds the OpenAI 300-char limit."""
    from mnemos.api.openapi_compat import GPT_ACTIONS_DESCRIPTION_LIMIT

    out = tmp_path / "gpt-spec.json"
    result = runner.invoke(
        app, ["dump-openapi", "--target", "gpt-actions", "-o", str(out)],
    )
    assert result.exit_code == 0, result.output
    spec = json.loads(out.read_text())

    over_limit = []
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in {
                "get", "put", "post", "delete", "options",
                "head", "patch", "trace",
            }:
                continue
            for field in ("summary", "description"):
                value = op.get(field) or ""
                if len(value) > GPT_ACTIONS_DESCRIPTION_LIMIT:
                    over_limit.append(
                        (method, path, field, len(value))
                    )
    assert over_limit == [], (
        f"--target gpt-actions still emits over-limit description "
        f"fields: {over_limit!r}"
    )


def test_gpt_actions_target_truncates_parameter_descriptions(tmp_path: Path):
    """Pin parameter description limit (700 chars) for the same
    reason."""
    from mnemos.api.openapi_compat import (
        GPT_ACTIONS_PARAMETER_DESCRIPTION_LIMIT,
    )

    out = tmp_path / "gpt-spec.json"
    result = runner.invoke(
        app, ["dump-openapi", "--target", "gpt-actions", "-o", str(out)],
    )
    assert result.exit_code == 0
    spec = json.loads(out.read_text())

    over_limit = []
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in {
                "get", "put", "post", "delete", "options",
                "head", "patch", "trace",
            }:
                continue
            for param in op.get("parameters") or []:
                if not isinstance(param, dict):
                    continue
                desc = param.get("description") or ""
                if len(desc) > GPT_ACTIONS_PARAMETER_DESCRIPTION_LIMIT:
                    over_limit.append((method, path, param.get("name"), len(desc)))
    assert over_limit == [], (
        f"--target gpt-actions still emits over-limit parameter "
        f"description fields: {over_limit!r}"
    )


def test_gpt_actions_target_truncates_request_body_descriptions(tmp_path: Path):
    from mnemos.api.openapi_compat import (
        GPT_ACTIONS_PARAMETER_DESCRIPTION_LIMIT,
    )

    out = tmp_path / "gpt-spec.json"
    result = runner.invoke(
        app, ["dump-openapi", "--target", "gpt-actions", "-o", str(out)],
    )
    assert result.exit_code == 0
    spec = json.loads(out.read_text())

    over_limit = []
    for path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() not in {
                "get", "put", "post", "delete", "options",
                "head", "patch", "trace",
            }:
                continue
            request_body = op.get("requestBody")
            if not isinstance(request_body, dict):
                continue
            desc = request_body.get("description") or ""
            if len(desc) > GPT_ACTIONS_PARAMETER_DESCRIPTION_LIMIT:
                over_limit.append((method, path, len(desc)))
    assert over_limit == []


def test_invalid_target_rejected():
    result = runner.invoke(
        app, ["dump-openapi", "--target", "swagger-2.0"],
    )
    assert result.exit_code != 0
    assert "target" in result.output.lower()


def test_default_target_is_full(tmp_path: Path):
    """Without --target, behaviour is unchanged from round-36 v1
    (full spec). Confirm via long-description presence — same
    detector as the full-target test, just driven by the default."""
    result_default = runner.invoke(app, ["dump-openapi"])
    assert result_default.exit_code == 0
    spec = json.loads(result_default.output)

    found_long = any(
        len((op.get("description") or "")) > 300
        for path_methods in (spec.get("paths") or {}).values()
        if isinstance(path_methods, dict)
        for method, op in path_methods.items()
        if method.lower() in {
            "get", "put", "post", "delete", "options",
            "head", "patch", "trace",
        }
    )
    assert found_long, (
        "default target should be 'full' (raw FastAPI spec, "
        "long descriptions intact); none found"
    )


# ── Helper-level coverage for the truncate function ──────────────────────


def test_truncate_helper_caps_at_limit():
    from mnemos.api.openapi_compat import _truncate

    out = _truncate("a" * 500, 300)
    assert len(out) == 300
    # Final char is the ellipsis to signal truncation.
    assert out.endswith("…")


def test_truncate_helper_short_text_unchanged():
    from mnemos.api.openapi_compat import _truncate

    assert _truncate("short", 300) == "short"


def test_truncate_helper_zero_limit():
    from mnemos.api.openapi_compat import _truncate

    assert _truncate("anything", 0) == ""


def test_truncate_helper_non_string_passthrough():
    from mnemos.api.openapi_compat import _truncate

    assert _truncate(None, 100) is None
    assert _truncate(42, 100) == 42


# ── --server-url: inject servers[0].url for downstream consumers ──────────
#
# FastAPI doesn't auto-populate the OpenAPI ``servers`` field;
# ``--server-url`` injects it so OpenAI Custom GPT Actions and
# similar consumers get a working artifact in one command.


def test_server_url_injected_when_flag_set(tmp_path: Path):
    out = tmp_path / "spec.json"
    result = runner.invoke(
        app,
        [
            "dump-openapi",
            "--server-url", "https://mnemos.example.com",
            "-o", str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    spec = json.loads(out.read_text())
    assert spec.get("servers") == [{"url": "https://mnemos.example.com"}]


def test_no_server_url_means_field_unset(tmp_path: Path):
    """Default behaviour: no --server-url, no servers field
    (or whatever FastAPI produced). Pin so the flag stays
    opt-in."""
    out = tmp_path / "spec.json"
    result = runner.invoke(app, ["dump-openapi", "-o", str(out)])
    assert result.exit_code == 0
    spec = json.loads(out.read_text())
    # FastAPI omits servers when none configured. If it ever
    # changes default behaviour, this test catches the drift.
    assert "servers" not in spec or spec["servers"] == [{"url": "/"}]


def test_server_url_works_with_gpt_actions_target(tmp_path: Path):
    """Combining --target gpt-actions + --server-url is the
    primary use case (Custom GPT Action upload)."""
    out = tmp_path / "spec.json"
    result = runner.invoke(
        app,
        [
            "dump-openapi",
            "--target", "gpt-actions",
            "--server-url", "https://mnemos.prod.example.com",
            "-o", str(out),
        ],
    )
    assert result.exit_code == 0
    spec = json.loads(out.read_text())
    assert spec["servers"] == [{"url": "https://mnemos.prod.example.com"}]


def test_empty_server_url_rejected():
    """Passing --server-url '' fails fast rather than producing
    a spec with an empty server URL."""
    result = runner.invoke(app, ["dump-openapi", "--server-url", " "])
    assert result.exit_code != 0
    assert "server-url" in result.output.lower()
