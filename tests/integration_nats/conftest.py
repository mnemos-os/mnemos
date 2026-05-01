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


# Collection hook: skip live-broker tests when there's no broker
# to talk to. Two acceptable broker sources:
#   1. MNEMOS_NATS_TEST_URL — operator-managed external broker.
#   2. nats-server binary on PATH (or MNEMOS_NATS_SERVER_BIN) —
#      managed-broker tests spawn their own subprocess.
#
# If EITHER source is available, don't blanket-skip the directory.
# Tests that depend on the static URL skip via the ``nats_url``
# fixture; tests that depend on the spawned subprocess skip via
# the ``managed_broker`` fixture. So the per-fixture skip logic
# routes each test to the right outcome.
#
# Path containment uses pathlib's relative_to / commonpath rather
# than string prefix — `tests/integration_nats_extra/` would match
# `startswith("tests/integration_nats")` and silently get skipped.
# pytest_collection_modifyitems in a subdirectory conftest sees
# ALL collected items (not just items "below" the conftest), so
# the path filter is required.
def pytest_collection_modifyitems(config, items):
    have_url = _broker_url() is not None
    have_bin = _nats_server_bin() is not None
    if have_url or have_bin:
        return
    here = Path(__file__).resolve().parent
    skip = pytest.mark.skip(
        reason=(
            "no NATS broker source available. Set MNEMOS_NATS_TEST_URL "
            "for operator-managed-broker tests, OR install nats-server "
            "(brew/apt/release) for managed-subprocess tests."
        )
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


# --- Managed-broker fixture (Audit Finding 11) --------------------------------
#
# Some outage-control tests (broker shutdown mid-consume, durable
# consumer deletion, partial outage) need to OWN the broker
# subprocess so they can stop/start/kill it. The static
# ``MNEMOS_NATS_TEST_URL`` path doesn't fit because the broker is
# operator-managed. The fixture below tries to spawn a
# ``nats-server`` subprocess on a free port. It skips with a clear
# message when the binary isn't available — operators install
# nats-server (`brew install nats-server` / NATS GitHub releases /
# `apt install nats-server`) to enable these tests.


import shutil  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from contextlib import closing  # noqa: E402


def _free_port() -> int:
    """Pick a likely-free TCP port for the broker to bind.

    Codex round-1 of the partial-outage slice flagged the
    bind-then-close pattern as race-prone: another process can
    claim the port between the close and nats-server's bind.
    Mitigations:
      1. Set SO_REUSEADDR=0 (default) so the port really is
         released.
      2. Caller wraps the spawn in a retry loop AND verifies
         readiness via an actual NATS connection handshake,
         not a raw TCP probe (so an unrelated listener that
         happened to grab the port is detected).
    The legacy bind-then-close-then-spawn approach stays
    here as a port HINT; the readiness handshake is what
    actually establishes "we own this port".
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _nats_server_bin() -> str | None:
    """Resolve nats-server binary path or None.

    Honors ``MNEMOS_NATS_SERVER_BIN`` env override for operators
    with a non-PATH install (Yocto edge images, custom builds).
    """
    override = os.environ.get("MNEMOS_NATS_SERVER_BIN", "").strip()
    if override and Path(override).is_file() and os.access(override, os.X_OK):
        return override
    return shutil.which("nats-server")


class ManagedBroker:
    """Pytest-owned nats-server subprocess.

    Exposes broker-lifecycle controls so partial-outage tests can
    exercise the consume-loop reconnect+backoff path against a real
    JetStream broker (not a fake). Methods:

      * pause()   — SIGSTOP. Connections stay open but no progress.
      * resume()  — SIGCONT. Continues from paused state.
      * kill()    — SIGKILL + waitpid. Use for hard-shutdown tests.
      * restart() — kill + spawn a new instance on the same port +
                    JetStream store dir, so durable consumers and
                    streams persist across the cycle.

    JetStream is enabled with a per-test ``--store_dir`` so multiple
    tests on the same dev box don't share state, and so a hard kill
    doesn't leak state into the next run.
    """

    def __init__(self, bin_path: str, port: int, store_dir: Path):
        self.bin_path = bin_path
        self.port = port
        self.store_dir = store_dir
        self.proc: subprocess.Popen | None = None
        self.url = f"nats://127.0.0.1:{port}"
        # Per-instance connection-level identity tag. Codex round-4
        # flagged that subprocess liveness alone is not proof of socket
        # ownership: an unrelated nats-server can already be bound to
        # the port we picked, our child can be briefly alive trying to
        # bind, and the handshake can succeed against the squatter
        # while ``proc.poll() is None`` is still true. Stamp each
        # spawned child with a unique --server_name and verify the
        # CONNECT reply carries the same name before declaring ready.
        self._server_name = f"mnemos-managed-{uuid.uuid4().hex}"

    def _start_proc(self) -> None:
        """Just spawn the nats-server child. Caller is responsible
        for the readiness check (sync or async)."""
        store_str = str(self.store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                self.bin_path,
                "-a", "127.0.0.1",
                "-p", str(self.port),
                "-js",
                "--store_dir", store_str,
                # Identity stamp checked by _probe_once() — see
                # __init__() comment on _server_name.
                "--server_name", self._server_name,
                # Quiet logging so the test output isn't drowned.
                "-l", "/dev/null",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _kill_after_failure(self) -> None:
        """Defensive subprocess teardown for a failed spawn."""
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.send_signal(signal.SIGKILL)
                self.proc.wait(timeout=2.0)
        except Exception:
            pass
        self.proc = None

    async def _probe_once(self) -> None:
        """One handshake + identity check. Raises on failure.

        Shared between sync and async readiness paths.

        Identity model (codex round-4): we cannot rely on subprocess
        liveness alone — an unrelated nats-server may already own the
        socket, our child can be briefly alive trying to bind, and the
        handshake can succeed against the squatter while
        ``proc.poll() is None`` is still true. Instead, each managed
        child is launched with a unique ``--server_name`` and we
        verify that the CONNECT response advertises the same name
        before declaring readiness.
        """
        import asyncio as _asyncio
        import nats

        client = await _asyncio.wait_for(
            nats.connect(servers=[self.url]), timeout=0.5
        )
        try:
            # Connection-level identity assertion. nats.py exposes the
            # server INFO response on the client; the field name has
            # been stable across 2.x. Probe the most-likely attributes
            # in order so the test stays robust if a release shuffles
            # the surface.
            info = (
                getattr(client, "_server_info", None)
                or getattr(client, "connected_server_info", None)
                or {}
            )
            advertised = None
            if isinstance(info, dict):
                advertised = info.get("server_name")
            else:
                advertised = getattr(info, "server_name", None)
            if advertised != self._server_name:
                raise RuntimeError(
                    "handshake reached the wrong nats-server: "
                    f"expected server_name={self._server_name!r}, "
                    f"got {advertised!r}. Another listener won the port."
                )
            # Belt-and-braces: also confirm our child has not exited
            # since we spawned it. Cheap, catches early-bind failures
            # that may not have surfaced in INFO yet.
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError(
                    "handshake succeeded but our subprocess is gone — "
                    "another nats-server won the port"
                )
        finally:
            await client.drain()

    def _spawn(self) -> None:
        """Spawn + sync-probe readiness. Used at fixture setup
        when there is no running event loop yet.

        Codex round-1 flagged two leak paths in the original:
          1. Spawn-then-readiness-fail orphaned the subprocess
             (caller's try/finally never ran). Fixed by the
             try/except around the readiness wait that
             explicitly kills the child on any failure.
          2. Raw TCP probe could pass against an unrelated
             listener that grabbed the port. Fixed by using a
             real nats.connect handshake.

        Codex round-2 flagged that the original also called
        ``asyncio.run()`` from inside what could be a coroutine
        (when restart() is invoked from a pytest-asyncio test).
        Fixed by splitting:
          * _spawn() — sync path, used from fixture setup (no
            running loop).
          * async_spawn() — async path, used from inside async
            tests so we can ``await`` instead of ``asyncio.run``.

        Codex round-3 flagged that the post-handshake child-
        liveness check was only on the async path; sync _spawn()
        could still accept a wrong nats-server that won the port
        race. Both paths now go through ``_probe_once()``.
        """
        import asyncio as _asyncio

        self._start_proc()
        try:
            deadline = time.monotonic() + 5.0
            last_exc: Exception | None = None
            while time.monotonic() < deadline:
                if self.proc and self.proc.poll() is not None:
                    raise RuntimeError(
                        f"nats-server exited early (code={self.proc.returncode})"
                    )
                try:
                    _asyncio.run(self._probe_once())
                    return
                except Exception as exc:
                    last_exc = exc
                    time.sleep(0.1)
            raise RuntimeError(
                f"nats-server at {self.url} did not become ready within 5s "
                f"(last probe error: {last_exc})"
            )
        except Exception:
            self._kill_after_failure()
            raise

    async def async_spawn(self) -> None:
        """Async-native variant of _spawn() — safe to call from
        inside a running asyncio loop (e.g., a pytest-asyncio
        test). Same readiness-handshake contract; no
        ``asyncio.run`` to clash with the running loop.
        """
        import asyncio as _asyncio

        self._start_proc()
        try:
            deadline = _asyncio.get_event_loop().time() + 5.0
            last_exc: Exception | None = None
            while _asyncio.get_event_loop().time() < deadline:
                if self.proc and self.proc.poll() is not None:
                    raise RuntimeError(
                        f"nats-server exited early (code={self.proc.returncode})"
                    )
                try:
                    await self._probe_once()
                    return
                except Exception as exc:
                    last_exc = exc
                    await _asyncio.sleep(0.1)
            raise RuntimeError(
                f"nats-server at {self.url} did not become ready within 5s "
                f"(last probe error: {last_exc})"
            )
        except Exception:
            self._kill_after_failure()
            raise

    def pause(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGSTOP)

    def resume(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGCONT)

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=2.0)

    def restart(self) -> None:
        """Sync hard-restart. Use from non-async contexts only —
        will raise if called from inside a running event loop
        because ``_spawn`` calls ``asyncio.run()`` for its
        readiness probe. From async tests, use ``async_restart``
        instead.
        """
        self.kill()
        self._spawn()

    async def async_restart(self) -> None:
        """Async-native hard-restart. Safe from inside
        pytest-asyncio coroutines.
        """
        self.kill()
        await self.async_spawn()


@pytest.fixture
def managed_broker(tmp_path: Path):
    """Pytest-owned nats-server lifecycle for outage tests.

    Each test gets its own broker subprocess + store_dir, so tests
    are independent. The fixture skips with a clear message if
    nats-server isn't installed (most operators won't have it on
    a default mnemos dev box).

    Port-race resilience: codex round-1 flagged that bind-then-
    close port allocation can lose to another process between
    the close and nats-server's bind. We retry up to 3 times
    with fresh ports — if all 3 attempts hit the race, the
    fixture surfaces the spawn error rather than mask it.
    """
    bin_path = _nats_server_bin()
    if bin_path is None:
        pytest.skip(
            "managed-broker tests need a nats-server binary. Install via "
            "`brew install nats-server` (macOS), `apt install nats-server` "
            "(Debian/Ubuntu), or a release from "
            "github.com/nats-io/nats-server. Or set "
            "MNEMOS_NATS_SERVER_BIN to an absolute path."
        )

    broker: ManagedBroker | None = None
    last_exc: Exception | None = None
    for attempt in range(3):
        port = _free_port()
        candidate = ManagedBroker(bin_path, port, tmp_path / f"jetstream-{attempt}")
        try:
            candidate._spawn()
            broker = candidate
            break
        except Exception as exc:
            last_exc = exc
            # candidate._spawn() already cleaned up its subprocess;
            # try a fresh port.
            continue

    if broker is None:
        raise RuntimeError(
            f"could not spawn nats-server after 3 port retries; "
            f"last error: {last_exc}"
        )

    try:
        yield broker
    finally:
        broker.kill()
