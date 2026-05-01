"""Regression tests for ``mnemos.nats.client.get_node_name``.

CORPUS-REVIEW-V4.2-NATS finding #9: when ``MNEMOS_NODE_NAME`` is
unset, the function falls back to ``socket.gethostname()`` which
can collide / change on restart / differ across blue-green
deploys, breaking federation self-loop checks and webhook durable
consumer names. Round-39 keeps the fallback (back-compat) but
adds a one-shot warning so operators see the fallback in
startup logs.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from mnemos.nats import client as nats_client


@pytest.fixture(autouse=True)
def _reset_fallback_flag(monkeypatch):
    """Each test starts with the one-shot warning flag cleared so
    successive tests can each observe the warning fire."""
    monkeypatch.setattr(nats_client, "_NODE_NAME_FALLBACK_LOGGED", False)
    yield


def test_explicit_node_name_does_not_warn(monkeypatch):
    """When MNEMOS_NODE_NAME is set, no fallback warning is emitted."""
    from mnemos.core import config

    monkeypatch.setenv("MNEMOS_NODE_NAME", "alpha-prod-1")
    monkeypatch.setattr(config, "_settings", None)

    with patch.object(nats_client.logger, "warning") as mock_warn:
        out = nats_client.get_node_name()
    assert out == "alpha-prod-1"
    mock_warn.assert_not_called()


def test_unset_node_name_warns_once_then_silent(monkeypatch):
    """Hostname fallback is allowed, but the one-shot warning fires
    on the first call and stays silent on subsequent calls in the
    same process."""
    from mnemos.core import config

    monkeypatch.delenv("MNEMOS_NODE_NAME", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    monkeypatch.setattr(nats_client.socket, "gethostname", lambda: "container-77ad")

    with patch.object(nats_client.logger, "warning") as mock_warn:
        out1 = nats_client.get_node_name()
        out2 = nats_client.get_node_name()
        out3 = nats_client.get_node_name()

    assert out1 == "container-77ad"
    assert out2 == "container-77ad"
    assert out3 == "container-77ad"
    assert mock_warn.call_count == 1, (
        f"expected exactly one fallback warning across 3 calls; got "
        f"{mock_warn.call_count}"
    )
    # Format-string + args check: the message format must mention
    # MNEMOS_NODE_NAME and the resolved hostname must be passed in.
    fmt = mock_warn.call_args.args[0]
    args = mock_warn.call_args.args[1:]
    assert "MNEMOS_NODE_NAME" in fmt
    assert "container-77ad" in args


def test_warning_includes_operator_guidance(monkeypatch):
    """The warning text must point operators at the env var to set
    so a fix is actionable from the log line alone."""
    from mnemos.core import config

    monkeypatch.delenv("MNEMOS_NODE_NAME", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    monkeypatch.setattr(nats_client.socket, "gethostname", lambda: "h1")

    with patch.object(nats_client.logger, "warning") as mock_warn:
        nats_client.get_node_name()

    assert mock_warn.call_count == 1
    fmt = mock_warn.call_args.args[0]
    # Operator-guidance substrings.
    assert "MNEMOS_NODE_NAME" in fmt
    assert "federation" in fmt.lower() or "durable" in fmt.lower()


def test_repeated_processes_each_warn_once(monkeypatch):
    """Simulate fresh process state by re-resetting the flag —
    confirms the warning is per-process, not per-import."""
    from mnemos.core import config

    monkeypatch.delenv("MNEMOS_NODE_NAME", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    monkeypatch.setattr(nats_client.socket, "gethostname", lambda: "h2")

    with patch.object(nats_client.logger, "warning") as mock_warn:
        nats_client.get_node_name()  # warning #1
        # Simulate process restart: reset both the one-shot warning
        # flag AND the persisted node_name (the first call mutated
        # settings.nats.node_name from "" to "h2", so without the
        # reset the second call's `.strip()` short-circuits past the
        # fallback path).
        nats_client._NODE_NAME_FALLBACK_LOGGED = False
        from mnemos.core.config import get_settings as _gs
        _gs().nats.node_name = ""
        nats_client.get_node_name()  # warning #2

    assert mock_warn.call_count == 2
