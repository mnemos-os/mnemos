"""Regression coverage for the ManagedBroker port-race identity check.

Codex round-4 of the partial-outage slice flagged that the previous
"is the subprocess still alive after the handshake?" check was not
actually a connection-level identity assertion — an unrelated
nats-server already bound to the selected port could win the race,
the handshake would succeed against it, and our briefly-alive child
would still poll() as None for a moment longer.

Fix: each managed child is spawned with ``--server_name=<uuid>``, and
``_probe_once()`` verifies the CONNECT INFO advertises the same name.

These tests reproduce the squatter scenario directly:

  1. ``test_probe_rejects_squatter`` — spawn an unrelated nats-server
     (different ``--server_name``) on a chosen port, point a separate
     ``ManagedBroker`` at the same port, run the probe, and assert it
     raises with the wrong-server error.

  2. ``test_probe_accepts_own_child`` — happy path: spawn a real
     managed broker, call probe, expect success.
"""
from __future__ import annotations

import asyncio
import subprocess
import time

import pytest

from .conftest import ManagedBroker, _free_port, _nats_server_bin


pytestmark = pytest.mark.asyncio


def _require_nats_server() -> str:
    bin_path = _nats_server_bin()
    if not bin_path:
        pytest.skip("nats-server binary not available")
    return bin_path


async def test_probe_accepts_own_child(tmp_path):
    """Sanity check: probe succeeds against the broker it spawned."""
    bin_path = _require_nats_server()
    port = _free_port()
    broker = ManagedBroker(bin_path, port, tmp_path / "store")
    try:
        await broker.async_spawn()
    finally:
        broker.kill()


async def test_probe_rejects_squatter(tmp_path):
    """Probe must reject a foreign nats-server occupying the port.

    We start a foreign server first with a deterministic identity,
    then construct a separate ManagedBroker pointed at the same port
    but never spawn its own child. Manually attaching a placeholder
    ``proc`` (a long-sleeping subprocess that polls() as alive)
    simulates the dangerous middle of the race window: our child is
    'still running' but does not own the socket. ``_probe_once()``
    must fail with the identity-mismatch error rather than declaring
    ready.
    """
    bin_path = _require_nats_server()
    port = _free_port()

    # Foreign server holds the port with its own server_name.
    foreign_store = tmp_path / "foreign-store"
    foreign_store.mkdir(parents=True)
    foreign = subprocess.Popen(
        [
            bin_path,
            "-a", "127.0.0.1",
            "-p", str(port),
            "-js",
            "--store_dir", str(foreign_store),
            "--server_name", "squatter-not-mnemos",
            "-l", "/dev/null",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Spin a moment so the foreign server is definitely accepting
    # connections before we probe.
    deadline = time.monotonic() + 5.0
    import nats
    while time.monotonic() < deadline:
        try:
            client = await asyncio.wait_for(
                nats.connect(servers=[f"nats://127.0.0.1:{port}"]),
                timeout=0.5,
            )
            await client.drain()
            break
        except Exception:
            await asyncio.sleep(0.1)
    else:
        foreign.kill()
        pytest.fail("foreign nats-server never came up")

    # Build a ManagedBroker that thinks it owns this port, but
    # attach an unrelated long-running subprocess as its proc so
    # ``poll() is None`` (the old liveness-only check) returns True.
    broker = ManagedBroker(bin_path, port, tmp_path / "broker-store")
    broker.proc = subprocess.Popen(
        ["sleep", "30"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with pytest.raises(RuntimeError, match="wrong nats-server"):
            await broker._probe_once()
    finally:
        broker._kill_after_failure()
        foreign.send_signal(15)
        try:
            foreign.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            foreign.kill()
            foreign.wait(timeout=2.0)
