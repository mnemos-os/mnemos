"""Regression coverage for the post-DB startup hook registry."""
from __future__ import annotations


def test_register_post_db_startup_hook_is_idempotent_by_name(monkeypatch):
    from mnemos.core import lifecycle

    monkeypatch.setattr(lifecycle, "_post_db_startup_hooks", {})

    async def first(_pool, _settings):
        return None

    async def second(_pool, _settings):
        return None

    lifecycle.register_post_db_startup_hook("federation nats consumers", first)
    lifecycle.register_post_db_startup_hook("federation nats consumers", second)

    # Second registration must REPLACE the first under the same name —
    # not append. Otherwise a module reload would queue duplicate
    # federation peer consumers / webhook trigger loops at next startup.
    assert list(lifecycle._post_db_startup_hooks.keys()) == [
        "federation nats consumers"
    ]
    assert lifecycle._post_db_startup_hooks["federation nats consumers"] is second


def test_register_post_db_startup_hook_keeps_distinct_names(monkeypatch):
    from mnemos.core import lifecycle

    monkeypatch.setattr(lifecycle, "_post_db_startup_hooks", {})

    async def federation_hook(_pool, _settings):
        return None

    async def webhook_hook(_pool, _settings):
        return None

    lifecycle.register_post_db_startup_hook("federation nats consumers", federation_hook)
    lifecycle.register_post_db_startup_hook("webhook nats trigger", webhook_hook)

    assert set(lifecycle._post_db_startup_hooks.keys()) == {
        "federation nats consumers",
        "webhook nats trigger",
    }
