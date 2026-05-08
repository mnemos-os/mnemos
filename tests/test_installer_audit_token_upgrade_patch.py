"""Installer slice #151: surgical audit-token patch on --upgrade.

#150 made fresh installs default-on for the
``/v1/internal/mcp_audit`` lockdown by autogenerating
``[server].internal_audit_token`` in config.toml. But operators
upgrading from v5.3.4 (where #148 added the env var but most
operators didn't set it) need the same default-on treatment via
``--upgrade`` — without that, the lockdown stays dormant after the
upgrade.

The surgical patcher mirrors ``_patch_config_toml_embedding_dim``:
parse with tomllib, surgically insert into the [server] block,
atomic ``install -m`` write preserving owner/group/mode. A no-op
when the token is already populated (operator-set or fresh-install).
"""
from __future__ import annotations

import os
import re

import pytest


@pytest.fixture(autouse=True)
def _clear_audit_env(monkeypatch):
    monkeypatch.delenv("MNEMOS_INTERNAL_AUDIT_TOKEN", raising=False)
    for k in ("MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE", "MNEMOS_CONFIG_PATH"):
        monkeypatch.delenv(k, raising=False)


def _read_token(text: str) -> str | None:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        parsed = tomllib.loads(text)
    except Exception:
        return None
    server = parsed.get("server")
    if not isinstance(server, dict):
        return None
    val = server.get("internal_audit_token")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return None


def test_patch_inserts_token_when_server_section_lacks_key(tmp_path):
    from mnemos.installer.__main__ import _patch_config_toml_internal_audit_token

    config_path = str(tmp_path / "config.toml")
    pre_existing = (
        "[server]\n"
        'profile = "edge"\n'
        "port = 5002\n"
        "\n"
        "[database]\n"
        'backend = "sqlite"\n'
        'sqlite_path = "/tmp/y.db"\n'
        "embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    assert _patch_config_toml_internal_audit_token(config_path) is True
    after = open(config_path).read()
    token = _read_token(after)
    assert token is not None
    assert len(token) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", token)


def test_patch_is_noop_when_token_already_populated(tmp_path):
    from mnemos.installer.__main__ import _patch_config_toml_internal_audit_token

    config_path = str(tmp_path / "config.toml")
    pre_existing = (
        "[server]\n"
        'profile = "edge"\n'
        "port = 5002\n"
        'internal_audit_token = "operator-wired-do-not-rotate"\n'
        "\n"
        "[database]\n"
        'backend = "sqlite"\n'
        "embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    assert _patch_config_toml_internal_audit_token(config_path) is True
    after = open(config_path).read()
    assert _read_token(after) == "operator-wired-do-not-rotate"


def test_patch_replaces_empty_quoted_value(tmp_path):
    """An empty-string token from a placeholder gets replaced."""
    from mnemos.installer.__main__ import _patch_config_toml_internal_audit_token

    config_path = str(tmp_path / "config.toml")
    pre_existing = (
        "[server]\n"
        'profile = "edge"\n'
        'internal_audit_token = ""\n'
        "port = 5002\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    assert _patch_config_toml_internal_audit_token(config_path) is True
    after = open(config_path).read()
    token = _read_token(after)
    assert token is not None
    assert len(token) == 64


def test_patch_replaces_empty_value_with_inline_comment(tmp_path):
    """Codex round-1 HIGH on #151: a common placeholder shape is
    `internal_audit_token = "" # placeholder` — tomllib parses this
    as empty (so populated-check correctly skips it), but the regex
    must also recognize the line for replacement; otherwise the
    patcher appends a duplicate live key, the validator fails with
    duplicate-key error, and --upgrade soft-warns into legacy mode."""
    from mnemos.installer.__main__ import _patch_config_toml_internal_audit_token

    config_path = str(tmp_path / "config.toml")
    pre_existing = (
        "[server]\n"
        'profile = "edge"\n'
        'internal_audit_token = "" # placeholder for operator\n'
        "port = 5002\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    assert _patch_config_toml_internal_audit_token(config_path) is True
    after = open(config_path).read()
    token = _read_token(after)
    assert token is not None
    assert len(token) == 64

    # No duplicate live key — exactly one non-comment line containing
    # `internal_audit_token = `.
    live_lines = [
        line for line in after.splitlines()
        if "internal_audit_token" in line and not line.lstrip().startswith("#")
    ]
    assert len(live_lines) == 1, (
        f"expected exactly 1 live internal_audit_token line, got "
        f"{len(live_lines)}:\n{live_lines}"
    )


def test_patch_appends_server_section_when_missing(tmp_path):
    from mnemos.installer.__main__ import _patch_config_toml_internal_audit_token

    config_path = str(tmp_path / "config.toml")
    pre_existing = (
        "[database]\n"
        'backend = "sqlite"\n'
        "embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    assert _patch_config_toml_internal_audit_token(config_path) is True
    after = open(config_path).read()
    token = _read_token(after)
    assert token is not None
    assert len(token) == 64
    # database section preserved.
    assert 'backend = "sqlite"' in after
    assert "embedding_dim = 768" in after


def test_patch_uses_env_token_when_set(monkeypatch, tmp_path):
    """When MNEMOS_INTERNAL_AUDIT_TOKEN is in the environment, it
    wins (operator rotation across upgrade)."""
    from mnemos.installer.__main__ import _patch_config_toml_internal_audit_token

    config_path = str(tmp_path / "config.toml")
    pre_existing = "[server]\nport = 5002\n"
    with open(config_path, "w") as f:
        f.write(pre_existing)

    monkeypatch.setenv("MNEMOS_INTERNAL_AUDIT_TOKEN", "rotated-via-upgrade-env")
    assert _patch_config_toml_internal_audit_token(config_path) is True
    after = open(config_path).read()
    assert _read_token(after) == "rotated-via-upgrade-env"


def test_patch_does_not_clobber_other_server_settings(tmp_path):
    """The surgical patcher must preserve port/profile/etc untouched."""
    from mnemos.installer.__main__ import _patch_config_toml_internal_audit_token

    config_path = str(tmp_path / "config.toml")
    pre_existing = (
        "[server]\n"
        'profile = "server"\n'
        "port = 5102\n"
        'base = "http://[::1]:5102"\n'
        "workers = 4\n"
        "\n"
        "[database]\n"
        'backend = "postgres"\n'
        'host = "localhost"\n'
        "embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    assert _patch_config_toml_internal_audit_token(config_path) is True
    after = open(config_path).read()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    parsed = tomllib.loads(after)
    assert parsed["server"]["profile"] == "server"
    assert parsed["server"]["port"] == 5102
    assert parsed["server"]["base"] == "http://[::1]:5102"
    assert parsed["server"]["workers"] == 4
    assert parsed["database"]["backend"] == "postgres"
    assert parsed["database"]["host"] == "localhost"


def test_patch_returns_false_on_unparseable_config(tmp_path):
    from mnemos.installer.__main__ import _patch_config_toml_internal_audit_token

    config_path = str(tmp_path / "config.toml")
    with open(config_path, "w") as f:
        f.write("not [valid toml = at all\n")

    # Soft-fail: returns False, original file untouched.
    assert _patch_config_toml_internal_audit_token(config_path) is False
    assert open(config_path).read() == "not [valid toml = at all\n"


def test_patch_returns_false_when_file_missing(tmp_path):
    from mnemos.installer.__main__ import _patch_config_toml_internal_audit_token

    missing_path = str(tmp_path / "nonexistent.toml")
    assert _patch_config_toml_internal_audit_token(missing_path) is False


def test_patch_preserves_file_mode(tmp_path):
    """The mode of the original config.toml must survive the patch."""
    from mnemos.installer.__main__ import _patch_config_toml_internal_audit_token

    config_path = str(tmp_path / "config.toml")
    pre_existing = "[server]\nport = 5002\n"
    with open(config_path, "w") as f:
        f.write(pre_existing)
    os.chmod(config_path, 0o640)

    assert _patch_config_toml_internal_audit_token(config_path) is True

    mode = os.stat(config_path).st_mode & 0o777
    assert mode == 0o640, f"expected 0o640, got {oct(mode)}"


def test_patch_idempotent_on_repeated_runs(tmp_path):
    """First run inserts; second run is a no-op (token unchanged)."""
    from mnemos.installer.__main__ import _patch_config_toml_internal_audit_token

    config_path = str(tmp_path / "config.toml")
    pre_existing = "[server]\nport = 5002\n"
    with open(config_path, "w") as f:
        f.write(pre_existing)

    assert _patch_config_toml_internal_audit_token(config_path) is True
    first = open(config_path).read()
    first_token = _read_token(first)
    assert first_token is not None

    assert _patch_config_toml_internal_audit_token(config_path) is True
    second = open(config_path).read()
    assert _read_token(second) == first_token
    # File content is byte-identical after second run (no rotation).
    assert first == second


def test_upgrade_path_calls_audit_token_patch_after_embedding_dim():
    """Source-level guard: the --upgrade branch must invoke
    `_patch_config_toml_internal_audit_token` after the embedding-dim
    refresh succeeds. Without this, an upgrade leaves
    /v1/internal/mcp_audit in legacy mode."""
    import inspect

    from mnemos.installer import __main__ as installer_main

    src = inspect.getsource(installer_main)
    upgrade_idx = src.find("if args.upgrade:")
    assert upgrade_idx != -1, "could not find --upgrade dispatcher"

    # Find the closing of that block: the next top-level def or
    # `else:` at the same indent. Heuristic: scan a generous window
    # — the upgrade dispatcher in __main__ runs ~200 lines including
    # both embedding_dim + audit_token patches.
    upgrade_block = src[upgrade_idx : upgrade_idx + 20000]

    embed_idx = upgrade_block.find("_patch_config_toml_embedding_dim")
    assert embed_idx != -1, "embedding_dim patch must remain in --upgrade"

    audit_idx = upgrade_block.find("_patch_config_toml_internal_audit_token")
    assert audit_idx != -1, (
        "_patch_config_toml_internal_audit_token must be called inside "
        "--upgrade so v5.3.4-era installs get the #150 default-on autogen"
    )
    # Must come AFTER embedding_dim patch (otherwise we'd patch the
    # token before the migration's vector-column reshape settled).
    assert audit_idx > embed_idx, (
        "audit-token patch must be called AFTER embedding_dim patch in "
        "--upgrade flow"
    )
