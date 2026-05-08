"""Slice #152: WARN at API startup when audit token is unset.

After #150/#151 the installer makes ``[server].internal_audit_token``
default-on for fresh installs and brings legacy installs up via
--upgrade. An unset token at API startup therefore means an
operator-initiated downgrade OR a partial/incomplete upgrade.
Surface this at boot so the operator notices that
``/v1/internal/mcp_audit`` is in legacy mode (any authenticated
bearer-token caller can POST audit rows).

We test the helper ``_warn_if_audit_token_unset`` directly to avoid
re-import + module-cache pollution that would disturb other tests.
The helper is called at module import time in ``mnemos.api.main`` —
the import-time call site is exercised by the rest of the API test
suite (which loads main and would surface the warning during fixture
setup).
"""
from __future__ import annotations

import logging
import re
import types

import pytest


def _make_settings(token: str | None) -> types.SimpleNamespace:
    """Build a minimal settings stub matching what the helper reads."""
    server = types.SimpleNamespace(internal_audit_token=token)
    return types.SimpleNamespace(server=server)


@pytest.fixture
def fresh_caplog(caplog):
    """Capture WARNINGs from mnemos.api.main, but force the module to
    be imported BEFORE we open the capture window. Otherwise a fresh
    import (when this test runs in isolation) fires the import-time
    `_warn_if_audit_token_unset(_settings)` call site, which emits
    the LEGACY warning into caplog before the test even runs its
    helper call — failing tests that assert no warnings."""
    # Trigger import-time side effects once, in this fixture, BEFORE
    # caplog starts recording.
    import mnemos.api.main  # noqa: F401
    caplog.set_level(logging.WARNING, logger="mnemos.api.main")
    caplog.clear()
    yield caplog


def test_warn_emits_when_token_is_none(fresh_caplog):
    from mnemos.api.main import _warn_if_audit_token_unset

    emitted = _warn_if_audit_token_unset(_make_settings(None))
    assert emitted is True

    legacy_warnings = [
        rec.message for rec in fresh_caplog.records
        if "LEGACY mode" in rec.message
        and "/v1/internal/mcp_audit" in rec.message
    ]
    assert legacy_warnings


def test_warn_emits_when_token_is_empty_string(fresh_caplog):
    from mnemos.api.main import _warn_if_audit_token_unset

    assert _warn_if_audit_token_unset(_make_settings("")) is True
    assert any(
        "LEGACY mode" in rec.message for rec in fresh_caplog.records
    )


def test_warn_emits_when_token_is_whitespace_only(fresh_caplog):
    from mnemos.api.main import _warn_if_audit_token_unset

    assert _warn_if_audit_token_unset(_make_settings("   \t\n  ")) is True
    assert any(
        "LEGACY mode" in rec.message for rec in fresh_caplog.records
    )


def test_warn_silent_when_token_is_set(fresh_caplog):
    from mnemos.api.main import _warn_if_audit_token_unset

    assert _warn_if_audit_token_unset(_make_settings("0" * 64)) is False
    legacy_warnings = [
        rec.message for rec in fresh_caplog.records
        if "LEGACY mode" in rec.message
    ]
    assert not legacy_warnings


# ────────────────────────────────────────────────────────────────────
# Slice #155: warn when a token IS set but is too short.
# ────────────────────────────────────────────────────────────────────


def test_warn_emits_when_token_is_too_short(fresh_caplog):
    """A configured-but-short token still engages the lockdown but
    is brute-forceable. Operator typo / placeholder shape — emit a
    distinct WARN so it's noticed."""
    from mnemos.api.main import _warn_if_audit_token_unset

    # 8 chars — way below the 32-char floor.
    assert _warn_if_audit_token_unset(_make_settings("abcdef12")) is True

    short_warnings = [
        rec.message for rec in fresh_caplog.records
        if "minimum recommended" in rec.message
        and "characters" in rec.message
    ]
    assert short_warnings, (
        f"expected a length-floor warning; got: "
        f"{[rec.message for rec in fresh_caplog.records]}"
    )


def test_warn_silent_at_minimum_length(fresh_caplog):
    """Exactly 32 chars (the configured floor) is not a warning."""
    from mnemos.api.main import _AUDIT_TOKEN_MIN_LENGTH, _warn_if_audit_token_unset

    token = "a" * _AUDIT_TOKEN_MIN_LENGTH
    assert _warn_if_audit_token_unset(_make_settings(token)) is False

    short_warnings = [
        rec.message for rec in fresh_caplog.records
        if "minimum recommended" in rec.message
    ]
    assert not short_warnings


def test_warn_emits_just_below_minimum_length(fresh_caplog):
    """One character below the floor still warns — defends the
    boundary."""
    from mnemos.api.main import _AUDIT_TOKEN_MIN_LENGTH, _warn_if_audit_token_unset

    token = "a" * (_AUDIT_TOKEN_MIN_LENGTH - 1)
    assert _warn_if_audit_token_unset(_make_settings(token)) is True

    short_warnings = [
        rec.message for rec in fresh_caplog.records
        if "minimum recommended" in rec.message
    ]
    assert short_warnings


def test_warn_distinguishes_unset_from_short(fresh_caplog):
    """The unset and too-short cases must emit DISTINCT messages so
    operators can disambiguate from logs."""
    from mnemos.api.main import _warn_if_audit_token_unset

    _warn_if_audit_token_unset(_make_settings(None))  # unset
    fresh_caplog.clear()
    _warn_if_audit_token_unset(_make_settings("short"))  # short

    short_msgs = [rec.message for rec in fresh_caplog.records]
    assert any("minimum recommended" in m for m in short_msgs)
    # NO "LEGACY mode" wording when the token is set-but-short — the
    # lockdown is engaged.
    assert not any("LEGACY mode" in m for m in short_msgs)


def test_warn_short_message_includes_remediation(fresh_caplog):
    """The short-token warning must point operators at a one-liner
    they can copy-paste to generate a strong replacement."""
    from mnemos.api.main import _warn_if_audit_token_unset

    _warn_if_audit_token_unset(_make_settings("xx"))
    full_text = " ".join(rec.message for rec in fresh_caplog.records)
    assert "secrets.token_hex" in full_text, (
        f"expected `secrets.token_hex` in remediation; got:\n{full_text}"
    )


def test_warn_message_names_remediation_paths(fresh_caplog):
    """The warning must surface BOTH remediation paths so operators
    can pick whichever fits their deployment shape: the installer
    --upgrade autogen or the env var override."""
    from mnemos.api.main import _warn_if_audit_token_unset

    _warn_if_audit_token_unset(_make_settings(None))
    full_text = " ".join(rec.message for rec in fresh_caplog.records)

    assert re.search(r"--upgrade", full_text), (
        f"warning must reference `--upgrade` autogen path; got:\n{full_text}"
    )
    assert "MNEMOS_INTERNAL_AUDIT_TOKEN" in full_text, (
        f"warning must reference the env var; got:\n{full_text}"
    )


def test_module_calls_helper_at_import_time():
    """Source-level guard: mnemos.api.main must invoke the helper at
    import time, not just expose it as a function. Without this, the
    warning would only fire when called by tests."""
    import inspect

    from mnemos.api import main as api_main

    src = inspect.getsource(api_main)
    # The function definition + an unconditional call site (not
    # inside another `def`/`class` block).
    assert "def _warn_if_audit_token_unset" in src, (
        "expected helper definition in mnemos.api.main"
    )
    # Find the call site outside of the def block.
    helper_def_idx = src.find("def _warn_if_audit_token_unset")
    helper_def_end = src.find("\n_oauth_state_secret", helper_def_idx)
    assert helper_def_end != -1, "expected def to be followed by other top-level code"
    after_def = src[helper_def_end:]
    assert "_warn_if_audit_token_unset(_settings)" in after_def, (
        "expected unconditional call site `_warn_if_audit_token_unset(_settings)` "
        "after the helper definition; otherwise the warning never fires."
    )
