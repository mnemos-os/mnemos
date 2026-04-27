"""GPUGuard — circuit-breaker state transitions.

Pure-logic tests for compression/gpu_guard.py. No HTTP, no real GPU.
State transitions are driven by direct record_success / record_failure
calls against controlled failure thresholds and cooldown windows.
"""

from __future__ import annotations

import asyncio

import pytest

from compression.gpu_guard import (
    CircuitState,
    GPUGuard,
    GuardConfig,
    all_guards,
    get_guard,
    reset_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


def _make(endpoint: str = "http://test:8000", **cfg) -> GPUGuard:
    base = GuardConfig()
    merged = GuardConfig(
        failure_threshold=cfg.get("failure_threshold", base.failure_threshold),
        cooldown_seconds=cfg.get("cooldown_seconds", base.cooldown_seconds),
        probe_timeout_seconds=cfg.get("probe_timeout_seconds", base.probe_timeout_seconds),
        log_throttle_seconds=cfg.get("log_throttle_seconds", base.log_throttle_seconds),
    )
    return GPUGuard(endpoint, config=merged)


# ---- initial state ---------------------------------------------------------


def test_initial_state_is_closed():
    guard = _make()
    assert guard.state is CircuitState.CLOSED
    assert asyncio.run(guard.is_available())[0] is True
    assert guard.last_error is None


# ---- CLOSED → OPEN ---------------------------------------------------------


def test_threshold_failures_open_the_circuit():
    guard = _make(failure_threshold=3)

    async def drive():
        for _ in range(3):
            await guard.record_failure(RuntimeError("oops"))

    asyncio.run(drive())
    assert guard.state is CircuitState.OPEN
    assert asyncio.run(guard.is_available())[0] is False
    assert guard.last_error is not None and "oops" in guard.last_error


def test_below_threshold_failures_stay_closed():
    guard = _make(failure_threshold=3)

    async def drive():
        await guard.record_failure(RuntimeError("one"))
        await guard.record_failure(RuntimeError("two"))

    asyncio.run(drive())
    assert guard.state is CircuitState.CLOSED
    assert asyncio.run(guard.is_available())[0] is True


def test_success_resets_failure_counter():
    guard = _make(failure_threshold=3)

    async def drive():
        await guard.record_failure(RuntimeError("one"))
        await guard.record_failure(RuntimeError("two"))
        await guard.record_success()
        # Counter reset — now need 3 more failures to open
        await guard.record_failure(RuntimeError("three"))
        await guard.record_failure(RuntimeError("four"))

    asyncio.run(drive())
    assert guard.state is CircuitState.CLOSED


# ---- OPEN → HALF_OPEN → CLOSED / OPEN --------------------------------------


def test_cooldown_elapses_to_half_open():
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)  # immediate cooldown

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        assert guard.state is CircuitState.OPEN
        # After any elapsed time >= 0, is_available() triggers HALF_OPEN
        admitted, _token = await guard.is_available()
        return admitted

    assert asyncio.run(drive()) is True
    assert guard.state is CircuitState.HALF_OPEN


def test_half_open_success_closes_circuit():
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        _admitted, token = await guard.is_available()  # -> HALF_OPEN
        await guard.record_success(probe_token=token)

    asyncio.run(drive())
    assert guard.state is CircuitState.CLOSED


def test_half_open_failure_reopens_circuit():
    # Small but non-zero cooldown: long enough that the re-opened
    # circuit stays OPEN when the post-probe is_available() fires
    # immediately; short enough the test finishes fast.
    guard = _make(failure_threshold=2, cooldown_seconds=0.1)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        # Wait past the cooldown window so the next is_available()
        # transitions OPEN -> HALF_OPEN.
        await asyncio.sleep(0.15)
        _admitted, token = await guard.is_available()
        assert _admitted is True
        assert guard.state is CircuitState.HALF_OPEN
        # Probe fails -> circuit re-opens with a FRESH cooldown window.
        await guard.record_failure(RuntimeError("probe failed"), probe_token=token)

    asyncio.run(drive())
    assert guard.state is CircuitState.OPEN
    # The fresh cooldown just started; is_available must still be False.
    assert asyncio.run(guard.is_available())[0] is False
    assert "probe failed" in (guard.last_error or "")


# ---- cooldown NOT elapsed keeps OPEN ---------------------------------------


def test_open_rejects_within_cooldown_window():
    # Large cooldown ensures we stay OPEN for the duration of the test.
    guard = _make(failure_threshold=2, cooldown_seconds=3600.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        return [(await guard.is_available())[0] for _ in range(3)]

    results = asyncio.run(drive())
    assert results == [False, False, False]
    assert guard.state is CircuitState.OPEN


# ---- snapshot + reset ------------------------------------------------------


def test_snapshot_shape():
    guard = _make(failure_threshold=2)

    async def drive():
        await guard.record_failure(RuntimeError("first"))

    asyncio.run(drive())

    snap = guard.snapshot()
    assert snap["endpoint"] == "http://test:8000"
    assert snap["state"] == "closed"
    assert snap["consecutive_failures"] == 1
    assert snap["opened_at"] is None
    assert "first" in snap["last_error"]
    assert snap["probe_in_flight"] is False
    assert snap["probe_started_at"] is None


def test_reset_returns_to_closed():
    guard = _make(failure_threshold=2, cooldown_seconds=3600.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))

    asyncio.run(drive())
    assert guard.state is CircuitState.OPEN

    guard.reset()
    assert guard.state is CircuitState.CLOSED
    assert guard.last_error is None
    assert asyncio.run(guard.is_available())[0] is True


# ---- registry --------------------------------------------------------------


def test_get_guard_dedupes_by_endpoint():
    a1 = get_guard("http://gpu.local:8000")
    a2 = get_guard("http://gpu.local:8000")
    assert a1 is a2


def test_get_guard_normalizes_trailing_slash():
    a1 = get_guard("http://gpu.local:8000")
    a2 = get_guard("http://gpu.local:8000/")
    assert a1 is a2


def test_get_guard_separates_different_endpoints():
    a = get_guard("http://host-a:8000")
    b = get_guard("http://host-b:8000")
    assert a is not b
    assert a.endpoint == "http://host-a:8000"
    assert b.endpoint == "http://host-b:8000"


def test_all_guards_returns_registered():
    get_guard("http://host-a:8000")
    get_guard("http://host-b:8000")
    snapshot = all_guards()
    assert set(snapshot.keys()) == {"http://host-a:8000", "http://host-b:8000"}


# ---- single-probe in HALF_OPEN (v3.1.1) -----------------------------------


def test_half_open_admits_only_one_concurrent_probe():
    """After cooldown elapses, the FIRST is_available() call transitions
    OPEN -> HALF_OPEN and returns True. Subsequent concurrent callers
    must see probe_in_flight and return False — otherwise we'd flood
    a possibly-still-broken endpoint.
    """
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        # First caller admitted as probe.
        first, first_token = await guard.is_available()
        # All subsequent callers rejected until probe resolves.
        second, _ = await guard.is_available()
        third, _ = await guard.is_available()
        return first, first_token, second, third

    first, first_token, second, third = asyncio.run(drive())
    assert first is True
    assert isinstance(first_token, int) and first_token > 0
    assert second is False
    assert third is False
    assert guard.state is CircuitState.HALF_OPEN
    assert guard.snapshot()["probe_in_flight"] is True


def test_probe_success_clears_in_flight_flag():
    """record_success must clear probe_in_flight so the next
    OPEN -> HALF_OPEN transition can admit a new probe.
    """
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        _admitted, token = await guard.is_available()  # probe admitted
        assert _admitted is True
        assert guard.snapshot()["probe_in_flight"] is True
        await guard.record_success(probe_token=token)
        assert guard.snapshot()["probe_in_flight"] is False
        assert guard.state is CircuitState.CLOSED

    asyncio.run(drive())


def test_probe_failure_clears_in_flight_flag_and_reopens():
    """record_failure while HALF_OPEN must both re-open the circuit
    AND clear probe_in_flight, so the NEXT cooldown cycle admits a
    fresh probe.
    """
    guard = _make(failure_threshold=2, cooldown_seconds=0.05)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        await asyncio.sleep(0.1)  # cooldown elapses
        _admitted, token = await guard.is_available()  # probe admitted
        assert _admitted is True
        assert guard.snapshot()["probe_in_flight"] is True
        await guard.record_failure(RuntimeError("probe died"), probe_token=token)
        # Circuit re-opened with FRESH cooldown; probe flag cleared.
        snap = guard.snapshot()
        assert snap["state"] == "open"
        assert snap["probe_in_flight"] is False
        # Within new cooldown, no admission.
        assert (await guard.is_available())[0] is False

    asyncio.run(drive())


def test_abandoned_probe_admits_replacement_after_timeout():
    """v3.2: probe-identity handshake makes auto-replacement safe.
    If a probe caller crashes/is cancelled without recording a result,
    the next is_available() after `probe_timeout_seconds` admits a
    replacement probe with a FRESH token. A late record_* call from
    the abandoned probe carries the stale token and is discarded.
    """
    guard = _make(
        failure_threshold=2,
        cooldown_seconds=0.05,
        probe_timeout_seconds=0.1,
    )

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        await asyncio.sleep(0.1)  # cooldown to reach HALF_OPEN
        # First probe admitted.
        admitted_a, token_a = await guard.is_available()
        assert admitted_a is True

        # Concurrent caller during probe window: rejected.
        rejected, rejected_token = await guard.is_available()
        assert rejected is False
        assert rejected_token is None

        # Wait past probe_timeout_seconds without reporting.
        await asyncio.sleep(0.15)

        # Replacement probe admitted with a FRESH token.
        admitted_b, token_b = await guard.is_available()
        assert admitted_b is True
        assert token_b != token_a

        # The original probe's late success arrives. Token is stale;
        # call is discarded, circuit stays HALF_OPEN.
        await guard.record_success(probe_token=token_a)
        assert guard.state is CircuitState.HALF_OPEN

        # The replacement probe's success with the current token
        # closes the circuit as intended.
        await guard.record_success(probe_token=token_b)
        assert guard.state is CircuitState.CLOSED

    asyncio.run(drive())


def test_reset_clears_probe_flags():
    """reset() must return the guard to a pristine state including
    probe-tracking fields.
    """
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        await guard.is_available()  # in_flight = True

    asyncio.run(drive())
    assert guard.snapshot()["probe_in_flight"] is True

    guard.reset()
    snap = guard.snapshot()
    assert snap["probe_in_flight"] is False
    assert snap["probe_started_at"] is None
    assert snap["state"] == "closed"


# ---- probe-identity handshake (v3.2) ---------------------------------------


def test_is_available_returns_token_on_half_open_admission():
    """First admission after cooldown returns a monotonically
    increasing non-zero token. Rejected callers get None."""
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        admitted, token = await guard.is_available()
        rejected, no_token = await guard.is_available()
        return admitted, token, rejected, no_token

    admitted, token, rejected, no_token = asyncio.run(drive())
    assert admitted is True
    assert isinstance(token, int) and token > 0
    assert rejected is False
    assert no_token is None


def test_closed_state_admission_returns_no_token():
    """CLOSED callers don't need a token — they're not probes."""
    guard = _make()

    async def drive():
        return await guard.is_available()

    admitted, token = asyncio.run(drive())
    assert admitted is True
    assert token is None


def test_stale_probe_success_discarded():
    """A late record_success from an abandoned probe carries a stale
    token; the call must NOT close the circuit if a replacement probe
    is already in flight.
    """
    guard = _make(failure_threshold=2, cooldown_seconds=0.0, probe_timeout_seconds=0.05)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        _, stale_token = await guard.is_available()
        await asyncio.sleep(0.1)  # past probe_timeout
        # Replacement probe admitted; stale token is now invalid.
        _, fresh_token = await guard.is_available()
        assert stale_token != fresh_token

        # Stale caller finally reports success — must be discarded.
        await guard.record_success(probe_token=stale_token)
        # Circuit still HALF_OPEN because the fresh probe hasn't resolved.
        assert guard.state is CircuitState.HALF_OPEN

    asyncio.run(drive())


def test_stale_probe_failure_discarded():
    """Mirror of success test: a late record_failure from an abandoned
    probe must not re-open a circuit that the replacement probe already
    closed."""
    guard = _make(failure_threshold=2, cooldown_seconds=0.0, probe_timeout_seconds=0.05)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        _, stale_token = await guard.is_available()
        await asyncio.sleep(0.1)
        _, fresh_token = await guard.is_available()
        # Replacement probe succeeds; circuit closes.
        await guard.record_success(probe_token=fresh_token)
        assert guard.state is CircuitState.CLOSED

        # Stale caller's late failure arrives. Must NOT re-open.
        await guard.record_failure(
            RuntimeError("stale failure"), probe_token=stale_token,
        )
        assert guard.state is CircuitState.CLOSED

    asyncio.run(drive())


def test_half_open_record_without_token_is_discarded():
    """HALF_OPEN probe resolution must include the admitted probe token."""
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        admitted, token = await guard.is_available()
        assert admitted is True
        assert token is not None
        await guard.record_success()
        assert guard.state is CircuitState.HALF_OPEN
        await guard.record_success(probe_token=token)

    asyncio.run(drive())
    assert guard.state is CircuitState.CLOSED


def test_reset_invalidates_outstanding_probe_token():
    """reset() must advance the token so any in-flight record_* with
    the old token is discarded after operator intervention."""
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        _, token_pre_reset = await guard.is_available()
        # Operator intervenes.
        guard.reset()
        # Old caller returns with success using the pre-reset token.
        await guard.record_success(probe_token=token_pre_reset)
        # Circuit stays CLOSED (reset's target) — the stale record is
        # discarded by the token check, not actioned.
        assert guard.state is CircuitState.CLOSED

    asyncio.run(drive())


def test_snapshot_exposes_probe_token():
    """Operators reading the snapshot JSON get both the derived
    `probe_in_flight` bool and the underlying `probe_token` int.
    """
    guard = _make(failure_threshold=2, cooldown_seconds=0.0)

    async def drive():
        await guard.record_failure(RuntimeError("a"))
        await guard.record_failure(RuntimeError("b"))
        await guard.is_available()  # admit probe

    asyncio.run(drive())
    snap = guard.snapshot()
    assert snap["probe_in_flight"] is True
    assert isinstance(snap["probe_token"], int) and snap["probe_token"] > 0
