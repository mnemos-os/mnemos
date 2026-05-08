"""Installer slice #150: install-time autogen of MNEMOS_INTERNAL_AUDIT_TOKEN.

Bounded follow-up to #148 (round-3 residual #1 of #146). Without
install-time autogeneration, operators must set
``MNEMOS_INTERNAL_AUDIT_TOKEN`` manually; most won't, so the trust
boundary on ``/v1/internal/mcp_audit`` stays in legacy mode (any
authenticated caller). With autogen, the lockdown is default-on for
new installs.

Tests cover:
1. Resolver returns a fresh token when no env / no existing config.
2. Env var wins when set.
3. Existing non-empty token in config.toml is preserved (don't clobber).
4. Empty existing token is replaced with a fresh one.
5. Generated tokens are 64-char hex strings.
6. _write_config_toml persists the token under [server].
7. _render_minimal_config emits the token under [server].
"""
from __future__ import annotations

import os
import re
from unittest.mock import patch

import pytest

from mnemos.installer.wizard import Config


@pytest.fixture(autouse=True)
def _clear_audit_env(monkeypatch):
    """Strip any env-side audit token so tests start from a clean slate."""
    monkeypatch.delenv("MNEMOS_INTERNAL_AUDIT_TOKEN", raising=False)
    for k in ("MNEMOS_PROFILE", "MNEMOS_PROFILE_OVERRIDE"):
        monkeypatch.delenv(k, raising=False)


def test_resolve_internal_audit_token_generates_fresh_when_no_inputs():
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    token = _resolve_internal_audit_token(None)
    assert token
    assert isinstance(token, str)
    # 256-bit hex = 64 chars
    assert len(token) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", token), (
        f"expected 64-char lowercase hex, got {token!r}"
    )


def test_resolve_internal_audit_token_env_wins():
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    expected = "operator-supplied-token-1234"
    with patch.dict(os.environ, {"MNEMOS_INTERNAL_AUDIT_TOKEN": expected}):
        # Existing config also has a token — env still wins.
        existing = '[server]\ninternal_audit_token = "old-token"\n'
        assert _resolve_internal_audit_token(existing) == expected


def test_resolve_internal_audit_token_blank_env_falls_through():
    """Empty/whitespace env var must NOT shadow existing config."""
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    existing = '[server]\ninternal_audit_token = "preserved-token"\n'
    with patch.dict(os.environ, {"MNEMOS_INTERNAL_AUDIT_TOKEN": "   "}):
        assert _resolve_internal_audit_token(existing) == "preserved-token"


def test_resolve_internal_audit_token_preserves_existing_value():
    """Re-running install must not rotate the operator's token."""
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    existing = (
        "[server]\n"
        'profile = "server"\n'
        'internal_audit_token = "abc123-existing"\n'
        "port = 5002\n"
    )
    assert _resolve_internal_audit_token(existing) == "abc123-existing"


def test_resolve_internal_audit_token_replaces_empty_existing():
    """An empty quoted token in config.toml is treated as 'not set' and
    a fresh value is generated."""
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    existing = '[server]\ninternal_audit_token = ""\n'
    token = _resolve_internal_audit_token(existing)
    assert token != ""
    assert len(token) == 64


def test_resolve_internal_audit_token_ignores_other_sections():
    """A field with the same name in a non-[server] section must NOT
    be picked up as the existing value."""
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    existing = (
        '[other]\ninternal_audit_token = "wrong-section"\n'
        "[server]\nprofile = \"edge\"\n"
    )
    token = _resolve_internal_audit_token(existing)
    # Should NOT pick up "wrong-section" — that's in [other].
    # Either generated fresh or the regex matched correctly.
    assert token != "wrong-section"


def test_resolve_internal_audit_token_handles_single_quoted_value(monkeypatch, tmp_path):
    """TOML allows single-quoted (literal) strings. Codex-flagged HIGH:
    the regex parser only recognized double quotes; tomllib handles
    both."""
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    # Point runtime resolver away from any other config.
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "no-such-config.toml"))
    existing = "[server]\ninternal_audit_token = 'literal-quoted-token'\n"
    assert _resolve_internal_audit_token(existing) == "literal-quoted-token"


def test_resolve_internal_audit_token_ipv6_url_does_not_break_lookup(
    monkeypatch, tmp_path
):
    """A bracketed string before the token (e.g. an IPv6 URL) must
    not confuse the section parser. Codex-flagged HIGH for the regex
    approach; tomllib handles it correctly."""
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "no-such-config.toml"))
    existing = (
        "[server]\n"
        'base = "http://[::1]:5002"\n'
        'internal_audit_token = "preserved-after-ipv6"\n'
    )
    assert _resolve_internal_audit_token(existing) == "preserved-after-ipv6"


def test_resolve_internal_audit_token_comment_with_brackets(monkeypatch, tmp_path):
    """A comment containing `[`/`]` before the token must not be
    interpreted as a section boundary."""
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "no-such-config.toml"))
    existing = (
        "[server]\n"
        "# notes [redacted] for this section\n"
        'internal_audit_token = "still-found"\n'
    )
    assert _resolve_internal_audit_token(existing) == "still-found"


def test_resolve_internal_audit_token_honors_mnemos_config_path(monkeypatch, tmp_path):
    """Codex-flagged HIGH: the resolver must read the file the runtime
    actually loads (MNEMOS_CONFIG_PATH wins) so a re-run install in a
    repo with no local config.toml — but with an existing token at
    /etc/mnemos/config.toml or wherever MNEMOS_CONFIG_PATH points —
    preserves that runtime-visible token instead of generating a
    fresh one (which would create token skew between API and bridges).
    """
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    runtime_config = tmp_path / "runtime-config.toml"
    runtime_config.write_text(
        "[server]\n"
        'profile = "server"\n'
        'internal_audit_token = "runtime-side-token"\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(runtime_config))

    # Caller has no in-memory existing content (e.g. brand-new repo
    # path with no config.toml). The runtime path's token must still
    # be picked up.
    assert _resolve_internal_audit_token(None) == "runtime-side-token"


def test_resolve_internal_audit_token_runtime_path_overrides_in_memory(
    monkeypatch, tmp_path
):
    """If both the runtime config AND the in-memory content carry a
    token, the runtime path wins — that's the file the service will
    actually read at startup."""
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    runtime_config = tmp_path / "runtime-config.toml"
    runtime_config.write_text(
        "[server]\n"
        'internal_audit_token = "runtime-token"\n'
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(runtime_config))

    in_memory = "[server]\ninternal_audit_token = \"stale-in-memory-token\"\n"
    assert _resolve_internal_audit_token(in_memory) == "runtime-token"


def test_resolve_internal_audit_token_malformed_runtime_falls_through(
    monkeypatch, tmp_path
):
    """If the runtime config exists but is malformed, the resolver
    must fall through to in-memory content / fresh-generate rather
    than crashing."""
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    runtime_config = tmp_path / "broken.toml"
    runtime_config.write_text("not [valid toml = at all\n")
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(runtime_config))

    in_memory = "[server]\ninternal_audit_token = \"in-memory-fallback\"\n"
    assert _resolve_internal_audit_token(in_memory) == "in-memory-fallback"


def test_resolve_internal_audit_token_malformed_in_memory_falls_through(
    monkeypatch, tmp_path
):
    """If the in-memory content is malformed and runtime has nothing,
    fresh-generate."""
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(tmp_path / "no-such-config.toml"))
    token = _resolve_internal_audit_token("not [valid toml = at all\n")
    assert len(token) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", token)


def test_resolve_internal_audit_token_returns_unique_tokens():
    """Two fresh generates should not collide (probabilistically)."""
    from mnemos.installer.__main__ import _resolve_internal_audit_token

    a = _resolve_internal_audit_token(None)
    b = _resolve_internal_audit_token(None)
    assert a != b


def test_render_minimal_config_emits_internal_audit_token():
    """Minimal-config path (no existing config.toml) embeds the token."""
    from mnemos.installer.__main__ import _render_minimal_config

    cfg = Config(profile="edge", embedding_dim=512, sqlite_path="/tmp/x.db")
    profile_defaults = {
        "backend": "sqlite",
        "rate_limit_storage": "memory://",
        "graeae_mode_default": "auto",
        "log_level": "INFO",
        "compression_workers": 1,
    }
    rendered = _render_minimal_config(cfg, profile_defaults)

    match = re.search(
        r'\[server\][^\[]*?internal_audit_token\s*=\s*"([0-9a-f]{64})"',
        rendered,
        flags=re.DOTALL,
    )
    assert match, (
        f"[server].internal_audit_token must be in the minimal config; got:\n{rendered}"
    )


def test_write_config_toml_emits_token_when_creating_new(tmp_path):
    """Fresh install (no existing config.toml, no example) writes the
    token under [server]."""
    from mnemos.installer.__main__ import _write_config_toml

    repo_path = str(tmp_path)
    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=5002,
    )
    _write_config_toml(cfg, repo_path)

    config_path = os.path.join(repo_path, "config.toml")
    assert os.path.exists(config_path)
    content = open(config_path).read()

    match = re.search(
        r'\[server\][^\[]*?internal_audit_token\s*=\s*"([0-9a-f]{64})"',
        content,
        flags=re.DOTALL,
    )
    assert match, (
        f"[server].internal_audit_token must be persisted; got:\n{content}"
    )


def test_write_config_toml_preserves_existing_token(tmp_path):
    """Re-running install on a config with an existing token must NOT
    rotate it (operator may have hand-set or env-derived a value)."""
    from mnemos.installer.__main__ import _write_config_toml

    repo_path = str(tmp_path)
    config_path = os.path.join(repo_path, "config.toml")
    pre_existing = (
        "[server]\n"
        'profile = "edge"\n'
        'internal_audit_token = "operator-wired-token-xyz"\n'
        "port = 5002\n"
        "\n"
        "[database]\n"
        'backend = "sqlite"\n'
        'sqlite_path = "/tmp/y.db"\n'
        "embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=5002,
    )
    _write_config_toml(cfg, repo_path)

    after = open(config_path).read()
    assert 'internal_audit_token = "operator-wired-token-xyz"' in after, (
        f"existing token must be preserved; got:\n{after}"
    )


def test_write_config_toml_env_var_overrides_existing_token(tmp_path):
    """If MNEMOS_INTERNAL_AUDIT_TOKEN is in the environment at install
    time, that wins over an existing value (operator is intentionally
    rotating)."""
    from mnemos.installer.__main__ import _write_config_toml

    repo_path = str(tmp_path)
    config_path = os.path.join(repo_path, "config.toml")
    pre_existing = (
        "[server]\n"
        'profile = "edge"\n'
        'internal_audit_token = "old-token"\n'
        "port = 5002\n"
        "\n"
        "[database]\n"
        'backend = "sqlite"\n'
        'sqlite_path = "/tmp/y.db"\n'
        "embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=5002,
    )
    with patch.dict(os.environ, {"MNEMOS_INTERNAL_AUDIT_TOKEN": "rotated-token"}):
        _write_config_toml(cfg, repo_path)

    after = open(config_path).read()
    assert 'internal_audit_token = "rotated-token"' in after
    assert "old-token" not in after


def test_write_config_toml_writes_with_restricted_perms(tmp_path):
    """The audit token is sensitive — config.toml must end up at 0600."""
    from mnemos.installer.__main__ import _write_config_toml

    repo_path = str(tmp_path)
    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=5002,
    )
    _write_config_toml(cfg, repo_path)

    config_path = os.path.join(repo_path, "config.toml")
    mode = os.stat(config_path).st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


# ────────────────────────────────────────────────────────────────────
# Round-3 fixes: bracket-safe writing + MNEMOS_CONFIG_PATH write target.
# ────────────────────────────────────────────────────────────────────


def test_write_config_toml_preserves_ipv6_url_in_server_section(tmp_path):
    """Codex round-3 HIGH: the previous regex used `[^[]*?` for the
    section span, so an IPv6 URL string like `"http://[::1]:5002"` in
    [server] would be mis-detected as a section boundary and the
    audit-token replacement could land in the wrong place. With
    line-anchored detection this works correctly and the result still
    parses as TOML."""
    from mnemos.installer.__main__ import _write_config_toml

    repo_path = str(tmp_path)
    config_path = os.path.join(repo_path, "config.toml")
    pre_existing = (
        "[server]\n"
        'profile = "edge"\n'
        'base = "http://[::1]:5002"\n'
        "port = 5002\n"
        'internal_audit_token = "preserve-me-please"\n'
        "\n"
        "[database]\n"
        'backend = "sqlite"\n'
        'sqlite_path = "/tmp/y.db"\n'
        "embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=5002,
    )
    _write_config_toml(cfg, repo_path)

    after = open(config_path).read()
    # Must still parse as TOML.
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    parsed = tomllib.loads(after)

    # Existing token preserved.
    assert parsed["server"]["internal_audit_token"] == "preserve-me-please"
    # IPv6 base preserved.
    assert parsed["server"]["base"] == "http://[::1]:5002"
    # database section still has its values.
    assert parsed["database"]["backend"] == "sqlite"
    assert parsed["database"]["embedding_dim"] == 768


def test_write_config_toml_preserves_section_with_bracketed_comment(tmp_path):
    """A comment containing `[redacted]` before the patched key must
    not be mistaken for a section boundary."""
    from mnemos.installer.__main__ import _write_config_toml

    repo_path = str(tmp_path)
    config_path = os.path.join(repo_path, "config.toml")
    pre_existing = (
        "[server]\n"
        'profile = "edge"\n'
        "# notes: see [redacted] doc for the cipher choice\n"
        "port = 5002\n"
        "\n"
        "[database]\n"
        'backend = "sqlite"\n'
        'sqlite_path = "/tmp/y.db"\n'
        "embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=5002,
    )
    _write_config_toml(cfg, repo_path)

    after = open(config_path).read()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    parsed = tomllib.loads(after)

    # internal_audit_token landed inside [server] (autogen).
    assert isinstance(parsed["server"].get("internal_audit_token"), str)
    assert len(parsed["server"]["internal_audit_token"]) == 64
    # Comment preserved (still in content).
    assert "[redacted]" in after
    # database section unaffected.
    assert parsed["database"]["embedding_dim"] == 768


def test_write_config_toml_honors_mnemos_config_path(monkeypatch, tmp_path):
    """Codex round-3 HIGH: write target must follow MNEMOS_CONFIG_PATH
    so the autogen lands in the file the runtime actually reads."""
    from mnemos.installer.__main__ import _write_config_toml

    runtime_dir = tmp_path / "etc-mnemos"
    runtime_dir.mkdir()
    runtime_config = runtime_dir / "config.toml"
    runtime_config.write_text(
        "[server]\n"
        'profile = "server"\n'
        "port = 5002\n"
        "\n"
        "[database]\n"
        'backend = "sqlite"\n'
        'sqlite_path = "/tmp/runtime.db"\n'
        "embedding_dim = 768\n"
    )
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", str(runtime_config))

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/runtime.db",
        listen_port=5002,
    )
    _write_config_toml(cfg, str(repo_dir))

    # The runtime config (the one MNEMOS_CONFIG_PATH points at) is
    # what got patched.
    runtime_after = runtime_config.read_text()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    parsed = tomllib.loads(runtime_after)
    assert isinstance(parsed["server"].get("internal_audit_token"), str)
    assert len(parsed["server"]["internal_audit_token"]) == 64

    # The repo_path config.toml was NOT created.
    repo_config = repo_dir / "config.toml"
    assert not repo_config.exists(), (
        f"repo config should not have been created when MNEMOS_CONFIG_PATH was "
        f"set; found:\n{repo_config.read_text() if repo_config.exists() else ''}"
    )


def test_write_config_toml_resolves_to_repo_path_when_no_env(monkeypatch, tmp_path):
    """When MNEMOS_CONFIG_PATH is unset, write target stays at
    repo_path/config.toml (preserves backward compat for default
    deployments)."""
    from mnemos.installer.__main__ import _write_config_toml

    monkeypatch.delenv("MNEMOS_CONFIG_PATH", raising=False)
    repo_path = str(tmp_path)
    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=5002,
    )
    _write_config_toml(cfg, repo_path)

    assert (tmp_path / "config.toml").exists()


def test_resolve_config_write_target_helper(monkeypatch, tmp_path):
    """Direct unit test of the write-target resolver."""
    from mnemos.installer.__main__ import _resolve_config_write_target

    # No env → repo_path/config.toml.
    monkeypatch.delenv("MNEMOS_CONFIG_PATH", raising=False)
    assert _resolve_config_write_target("/path/to/repo") == "/path/to/repo/config.toml"

    # Env set → use it.
    target = str(tmp_path / "custom.toml")
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", target)
    assert _resolve_config_write_target("/path/to/repo") == target

    # Whitespace-only env falls through to repo_path.
    monkeypatch.setenv("MNEMOS_CONFIG_PATH", "   ")
    assert _resolve_config_write_target("/path/to/repo") == "/path/to/repo/config.toml"


def test_write_config_toml_ignores_commented_internal_audit_token_key(tmp_path):
    """Codex round-4 HIGH: a `# internal_audit_token = ""` comment in
    [server] must NOT satisfy the key regex. Without lookbehind on
    the key, the lazy `.*?` would consume `# `, the rest matches the
    comment as if it were the key, and `re.subn` returns n=1 — so the
    append path never runs and the resulting config has NO live
    [server].internal_audit_token, leaving /v1/internal/mcp_audit in
    legacy mode after a "successful" install."""
    from mnemos.installer.__main__ import _write_config_toml

    repo_path = str(tmp_path)
    config_path = os.path.join(repo_path, "config.toml")
    pre_existing = (
        "[server]\n"
        'profile = "edge"\n'
        '# internal_audit_token = ""  # placeholder for operator\n'
        "port = 5002\n"
        "\n"
        "[database]\n"
        'backend = "sqlite"\n'
        'sqlite_path = "/tmp/y.db"\n'
        "embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=5002,
    )
    _write_config_toml(cfg, repo_path)

    after = open(config_path).read()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    parsed = tomllib.loads(after)

    # The KEY (live, parsed) must exist + be a 64-char hex token.
    token = parsed["server"]["internal_audit_token"]
    assert isinstance(token, str)
    assert len(token) == 64
    assert re.fullmatch(r"[0-9a-f]{64}", token)

    # The comment must still exist in the raw text (unchanged) —
    # we don't rewrite it, we just append a real key on its own line.
    assert '# internal_audit_token = ""' in after


def test_write_config_toml_ignores_commented_keys_for_other_fields(tmp_path):
    """Same regression with a different key — the line-anchored fix
    affects ALL fields, not just internal_audit_token. A commented-out
    `port = ...` line must not be replaced; the live `port` line is."""
    from mnemos.installer.__main__ import _write_config_toml

    repo_path = str(tmp_path)
    config_path = os.path.join(repo_path, "config.toml")
    pre_existing = (
        "[server]\n"
        'profile = "edge"\n'
        "# port = 9999  # old setting\n"
        "port = 5002\n"
        "\n"
        "[database]\n"
        'backend = "sqlite"\n'
        'sqlite_path = "/tmp/y.db"\n'
        "embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=4242,  # new port — should land on the live `port` line
    )
    _write_config_toml(cfg, repo_path)

    after = open(config_path).read()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    parsed = tomllib.loads(after)

    # Live port updated to 4242.
    assert parsed["server"]["port"] == 4242
    # Commented `# port = 9999 ...` line preserved verbatim.
    assert "# port = 9999" in after


def test_write_config_toml_handles_indented_existing_keys(tmp_path):
    """Codex round-5 HIGH: TOML allows leading whitespace on keys.
    A bare `(?<=\\n)` lookbehind broke valid indented configs:
    `[server]\\n  internal_audit_token = "old"` would not match the
    replace regex (char before key is space, not newline), the append
    path would add an unindented duplicate, and tomllib would reject
    the result with `Cannot overwrite a value`. The fix consumes
    `\\n[ \\t]*` (newline + optional indent) before the key, which
    preserves the indent in the rewrite."""
    from mnemos.installer.__main__ import _write_config_toml

    repo_path = str(tmp_path)
    config_path = os.path.join(repo_path, "config.toml")
    pre_existing = (
        "[server]\n"
        '  profile = "edge"\n'
        "  port = 5002\n"
        '  internal_audit_token = "indented-existing-token"\n'
        "\n"
        "[database]\n"
        '  backend = "sqlite"\n'
        '  sqlite_path = "/tmp/y.db"\n'
        "  embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=5002,
    )
    _write_config_toml(cfg, repo_path)

    after = open(config_path).read()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    parsed = tomllib.loads(after)

    # Existing token preserved (resolver picked it up, _set replaced
    # the indented line in place).
    assert parsed["server"]["internal_audit_token"] == "indented-existing-token"
    # Indent preserved in the patched line — defensive check that we
    # didn't drop the leading 2 spaces.
    assert '  internal_audit_token = "indented-existing-token"' in after
    # No duplicated unindented `internal_audit_token = ...` line at column 0.
    lines_with_key = [
        line for line in after.splitlines()
        if "internal_audit_token" in line and not line.lstrip().startswith("#")
    ]
    assert len(lines_with_key) == 1, (
        f"expected exactly 1 live internal_audit_token line, got "
        f"{len(lines_with_key)}:\n{lines_with_key}"
    )


def test_write_config_toml_emits_real_key_when_no_existing_section_header(
    tmp_path,
):
    """Defensive: if [server] is missing entirely, the section is
    appended with the real key — not just a comment."""
    from mnemos.installer.__main__ import _write_config_toml

    repo_path = str(tmp_path)
    config_path = os.path.join(repo_path, "config.toml")
    pre_existing = (
        "[database]\n"
        'backend = "sqlite"\n'
        'sqlite_path = "/tmp/y.db"\n'
        "embedding_dim = 768\n"
    )
    with open(config_path, "w") as f:
        f.write(pre_existing)

    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=5002,
    )
    _write_config_toml(cfg, repo_path)

    after = open(config_path).read()
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]
    parsed = tomllib.loads(after)

    assert isinstance(parsed["server"]["internal_audit_token"], str)
    assert len(parsed["server"]["internal_audit_token"]) == 64


def test_write_config_toml_validates_result_parses_as_toml(monkeypatch, tmp_path):
    """Post-patch invariant: the result must be parseable TOML. If
    the regex patcher slips and produces malformed content, the
    write must fail loudly with the original file unchanged."""
    from mnemos.installer.__main__ import _write_config_toml

    # We'll force a malformed result by monkey-patching the _set
    # path indirectly: write a config that, if the patcher misbehaves,
    # would survive only because of the validator. We instead patch
    # _toml_value to inject invalid TOML.
    from mnemos.installer import __main__ as installer_main

    repo_path = str(tmp_path)
    config_path = os.path.join(repo_path, "config.toml")
    with open(config_path, "w") as f:
        f.write(
            "[server]\nprofile = \"edge\"\n"
            "[database]\nbackend = \"sqlite\"\nsqlite_path = \"/tmp/y.db\"\n"
            "embedding_dim = 768\n"
        )
    original = open(config_path).read()

    cfg = Config(
        profile="edge",
        embedding_dim=768,
        sqlite_path="/tmp/y.db",
        listen_port=5002,
    )

    # Inject a malformed value via a deliberately broken TOML escape
    # sequence — embedded raw quotes and newlines would break the
    # tomllib parser if the patch landed.
    real_resolve = installer_main._resolve_internal_audit_token
    monkeypatch.setattr(
        installer_main,
        "_resolve_internal_audit_token",
        lambda *_, **__: 'not-actually-quoted"\nbroken = oops',
    )
    try:
        with pytest.raises(RuntimeError, match="failed TOML parse"):
            _write_config_toml(cfg, repo_path)
    finally:
        monkeypatch.setattr(
            installer_main, "_resolve_internal_audit_token", real_resolve
        )

    # Original file unchanged.
    assert open(config_path).read() == original
