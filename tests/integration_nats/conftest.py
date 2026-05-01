"""Pytest fixtures for live-broker NATS integration tests.

Tests in this directory all require a real NATS broker. They auto-skip
when ``MNEMOS_NATS_TEST_URL`` is unset so the default ``pytest`` run
on a dev box without nats-server installed stays green.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio


def _broker_url() -> str | None:
    return os.environ.get("MNEMOS_NATS_TEST_URL", "").strip() or None


def _broker_token() -> str | None:
    return os.environ.get("MNEMOS_NATS_TEST_TOKEN", "").strip() or None


# Collection hook: skip ONLY the live-broker tests in this directory
# when MNEMOS_NATS_TEST_URL is unset. Critically, this filters by
# item path because pytest's plugin contract says
# pytest_collection_modifyitems in a subdirectory conftest still
# sees ALL collected items (not just items "below" the conftest).
# Without the path filter the hook would skip the entire test suite.
#
# Path containment uses pathlib's relative_to / commonpath rather
# than string prefix — `tests/integration_nats_extra/` would match
# `startswith("tests/integration_nats")` and silently get skipped.
def pytest_collection_modifyitems(config, items):
    if _broker_url():
        return
    here = Path(__file__).resolve().parent
    skip = pytest.mark.skip(
        reason="MNEMOS_NATS_TEST_URL not set — live-broker tests require a real NATS server"
    )
    for item in items:
        raw_path = getattr(item, "path", None) or getattr(item, "fspath", None)
        if raw_path is None:
            continue
        try:
            item_path = Path(str(raw_path)).resolve()
        except OSError:
            continue
        try:
            item_path.relative_to(here)
        except ValueError:
            continue
        item.add_marker(skip)


@pytest.fixture(scope="session")
def nats_url() -> str:
    """The configured live broker URL."""
    url = _broker_url()
    if not url:  # pragma: no cover — collection guard above already skipped
        pytest.skip("MNEMOS_NATS_TEST_URL not set")
    return url


@pytest.fixture(scope="session")
def nats_token() -> str | None:
    return _broker_token()


@pytest_asyncio.fixture
async def nc(nats_url: str, nats_token: str | None) -> AsyncIterator:
    """A short-lived NATS connection for one test."""
    import nats

    kwargs: dict = {"servers": [nats_url]}
    if nats_token:
        kwargs["token"] = nats_token
    connection = await nats.connect(**kwargs)
    try:
        yield connection
    finally:
        try:
            await connection.drain()
        except Exception:
            try:
                await connection.close()
            except Exception:
                pass


@pytest_asyncio.fixture
async def js(nc):
    """A JetStream context bound to the test connection."""
    return nc.jetstream()


@pytest.fixture
def test_stream_name() -> str:
    """Per-test isolated stream name.

    A random suffix prevents one test's stream lingering from leaking
    state into the next test (or one CI run leaking into the next).
    Tests are responsible for deleting their stream in a finalizer or
    teardown — see :func:`stream_cleanup`.
    """
    return f"MNEMOS_TEST_STREAM_{secrets.token_hex(4).upper()}"


@pytest_asyncio.fixture
async def stream_cleanup(js, test_stream_name: str):
    """Per-test stream cleanup.

    Swallows ONLY not-found errors (the test never created the stream,
    or it was already deleted elsewhere). All other exceptions —
    auth-revoked teardown, broker drop mid-cleanup, real NATS errors —
    propagate so the suite stays honest about its leak rate. Without
    this discipline a token that can create but not delete streams
    would leave random `MNEMOS_TEST_STREAM_*` consumers behind on
    every run while the suite stays green.
    """
    from nats.js.errors import NotFoundError

    yield test_stream_name
    try:
        await js.delete_stream(test_stream_name)
    except NotFoundError:
        pass
