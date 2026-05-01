"""Live-broker integration tests for the NATS JetStream substrate.

These tests require a real NATS server and are SKIPPED by default.
Set ``MNEMOS_NATS_TEST_URL=nats://host:4222`` (and optionally
``MNEMOS_NATS_TEST_TOKEN``) to enable.

Audit Finding 11 — pre-v4.2.0a9 the test suite covered happy-path
publish/subscribe and the reconnect-backoff scheduler with fakes,
but NEVER exercised stream-drift, subscribe-failure cleanup, or
partial-broker-outage paths against a live broker. These tests
fill that gap.

Operators rolling out queue-group support (v4.2.0a8) can also use
this suite as a smoke check against their cluster:

    MNEMOS_NATS_TEST_URL=nats://prod-broker:4222 \
        pytest tests/integration_nats/ -v
"""
