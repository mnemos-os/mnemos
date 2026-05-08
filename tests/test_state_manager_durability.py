"""StateManager.set durability invariants.

Round-5 of the v4.2.0a5 server slice surfaced that the cache update
was running BEFORE serialization + durable write, so callers could
see a "successful" cached value while Postgres still held the old
row (or no row). This file pins the post-fix ordering: serialize ->
durable write -> cache update, and assertions that a failure at
either step does NOT mutate the cache.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from mnemos.core.auth_context import UserContext
from mnemos.domain.memory_categorization.state import StateManager


class _FakeTx:
    async def start(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


def _user(user_id: str = "alice", namespace: str = "ns") -> UserContext:
    return UserContext(
        user_id=user_id,
        group_ids=[],
        role="user",
        namespace=namespace,
        authenticated=True,
    )


class _FailingConn:
    """Async context-manager + execute that always raises."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated DB write failure")

    async def fetchrow(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated DB write failure")

    def transaction(self) -> _FakeTx:
        return _FakeTx()


class _FailingPool:
    def acquire(self):
        return _FailingConn()


class _RecordingConn:
    def __init__(self) -> None:
        self.execute_calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def execute(self, *args: Any, **_kwargs: Any) -> None:
        self.execute_calls.append(args)

    async def fetchrow(self, *args: Any, **_kwargs: Any) -> Any:
        self.execute_calls.append(args)
        return {"value": args[-1]}

    def transaction(self) -> _FakeTx:
        return _FakeTx()


class _RecordingPool:
    def __init__(self) -> None:
        self.conn = _RecordingConn()

    def acquire(self):
        return self.conn


@pytest.mark.asyncio
async def test_set_propagates_db_failure_and_does_not_mutate_cache():
    pool = _FailingPool()
    mgr = StateManager(db_pool=pool)
    user = _user()

    # Pre-populate the cache via in-memory-only path (no pool):
    mem_only = StateManager(db_pool=None)
    await mem_only.set("k", "old-cached", user=user)
    assert mem_only._cache[(user.user_id, user.namespace, "k")] == "old-cached"

    # Now exercise the failing-pool branch on a separate manager
    with pytest.raises(RuntimeError, match="simulated DB write failure"):
        await mgr.set("k", "new-but-doomed", user=user)
    assert (user.user_id, user.namespace, "k") not in mgr._cache, (
        "cache must NOT reflect a value that did not commit to the DB"
    )


@pytest.mark.asyncio
async def test_set_propagates_serialization_failure_before_touching_cache():
    pool = _RecordingPool()
    mgr = StateManager(db_pool=pool)
    user = _user()

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        await mgr.set("k", Unserializable(), user=user)

    assert (user.user_id, user.namespace, "k") not in mgr._cache, (
        "cache must NOT reflect a value that failed to serialize"
    )
    assert pool.conn.execute_calls == [], "no DB write should have been attempted"


@pytest.mark.asyncio
async def test_set_updates_cache_only_after_durable_write_succeeds():
    pool = _RecordingPool()
    mgr = StateManager(db_pool=pool)
    user = _user()

    await mgr.set("k", {"complex": [1, 2, 3]}, user=user)

    # Cache reflects the just-stored value.
    assert mgr._cache[(user.user_id, user.namespace, "k")] == {"complex": [1, 2, 3]}
    # Exactly one DB write happened.
    assert len(pool.conn.execute_calls) == 1
    # Serialized payload uses the StateManager TEXT-prefix envelope
    # ("MNEMOS_SM:v1:" + json.dumps(value)) so reads can branch on
    # the prefix without needing to parse JSON to recognize our
    # rows. The prefix is non-JSON, so caller payloads of any shape
    # cannot collide.
    args = pool.conn.execute_calls[0]
    serialized = args[-1]
    assert isinstance(serialized, str)
    assert serialized.startswith("MNEMOS_SM:v1:")
    suffix = serialized[len("MNEMOS_SM:v1:"):]
    assert json.loads(suffix) == {"complex": [1, 2, 3]}


@pytest.mark.asyncio
async def test_set_in_memory_only_mode_does_update_cache():
    # Without a DB pool, the cache IS the persistent store; ordering
    # rule still requires successful serialization first.
    mgr = StateManager(db_pool=None)
    user = _user()
    await mgr.set("k", "hello", user=user)
    assert mgr._cache[(user.user_id, user.namespace, "k")] == "hello"


class _SlowConn:
    """Conn wrapper that defers fetchrow until a gate event fires."""

    def __init__(self, gate, inner):
        self._gate = gate
        self._inner = inner

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def fetchrow(self, *args, **kwargs):
        # Hold the lock-bound fetchrow until the gate is set.
        await self._gate.wait()
        async with self._inner as real:
            return await real.fetchrow(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        async with self._inner as real:
            return await real.execute(*args, **kwargs)

    async def fetch(self, *args, **kwargs):
        async with self._inner as real:
            return await real.fetch(*args, **kwargs)

    def transaction(self) -> _FakeTx:
        return _FakeTx()


class _CapturingPool:
    """Pool that records the last-written serialized value and replays
    it as the row a subsequent fetchrow returns. Models a fresh manager
    seeing the previous process's persisted bytes, i.e. a cold cache
    after restart."""

    def __init__(self) -> None:
        self.stored_text = None

    def acquire(self):
        return _CapturingConn(self)


class _CapturingConn:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def execute(self, *args: Any, **_kwargs: Any) -> None:
        self.pool.stored_text = args[-1]

    async def fetchrow(self, *_args: Any, **_kwargs: Any) -> Any:
        if len(_args) >= 5:
            self.pool.stored_text = _args[-1]
            return {"value": self.pool.stored_text}
        if self.pool.stored_text is None:
            return None
        return {"value": self.pool.stored_text}

    async def fetch(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        if self.pool.stored_text is None:
            return []
        return [{"key": "k", "updated": None}]

    def transaction(self) -> _FakeTx:
        return _FakeTx()


@pytest.mark.asyncio
async def test_cold_cache_get_round_trips_dict_back_to_dict():
    pool = _CapturingPool()
    writer = StateManager(db_pool=pool)
    user = _user()

    payload = {"identity": "alice", "machine_id": "macbook"}
    await writer.set("ident", payload, user=user)

    # Fresh manager simulates a cold cache (process restart, other
    # worker, etc). The stored bytes must rehydrate as the original
    # dict shape, NOT as the JSON-text representation.
    reader = StateManager(db_pool=pool)
    got = await reader.get("ident", user=user)
    assert got == payload
    assert isinstance(got, dict)


@pytest.mark.asyncio
async def test_cold_cache_get_round_trips_string_without_extra_quotes():
    pool = _CapturingPool()
    writer = StateManager(db_pool=pool)
    user = _user()

    await writer.set("greeting", "hello", user=user)

    reader = StateManager(db_pool=pool)
    got = await reader.get("greeting", user=user)
    # Round-trip must NOT yield '"hello"' (the JSON-quoted form).
    assert got == "hello"


@pytest.mark.asyncio
async def test_warm_and_cold_cache_agree_on_tuple_shape():
    # JSON round-trip collapses tuples to lists. Cache must store the
    # canonical (post-json) shape so warm and cold reads agree.
    pool = _CapturingPool()
    writer = StateManager(db_pool=pool)
    user = _user()
    await writer.set("coords", (1, 2, 3), user=user)

    warm = await writer.get("coords", user=user)
    cold = await StateManager(db_pool=pool).get("coords", user=user)
    assert warm == cold == [1, 2, 3]
    assert isinstance(warm, list)
    assert isinstance(cold, list)


@pytest.mark.asyncio
async def test_warm_and_cold_cache_agree_on_int_dict_keys():
    # JSON only allows string dict keys. {1: "a"} -> '{"1": "a"}' ->
    # {"1": "a"}. Cache must reflect the canonical (string-keyed)
    # shape, not the input integer-keyed shape.
    pool = _CapturingPool()
    writer = StateManager(db_pool=pool)
    user = _user()
    await writer.set("indexed", {1: "a", 2: "b"}, user=user)

    warm = await writer.get("indexed", user=user)
    cold = await StateManager(db_pool=pool).get("indexed", user=user)
    assert warm == cold == {"1": "a", "2": "b"}


@pytest.mark.asyncio
async def test_documented_rest_scalar_tradeoff_true_returns_string():
    # DOCUMENTED TRADEOFF: REST /v1/state writes scalar values
    # without the StateManager envelope. The on-disk shape "true"
    # is indistinguishable from a legacy opaque TEXT row also
    # containing "true" (perfectly valid pre-migration JSONB
    # scalar OR opaque text written by external tooling). We
    # preserve raw bytes for scalars to avoid silently retyping
    # legacy opaque rows. Callers that need typed scalar
    # round-tripping through StateManager should use
    # StateManager.set, which writes the envelope.
    pool = _CapturingPool()
    pool.stored_text = "true"
    got = await StateManager(db_pool=pool).get("flag", user=_user())
    assert got == "true"
    assert not isinstance(got, bool)


@pytest.mark.asyncio
async def test_documented_rest_scalar_tradeoff_42_returns_string():
    pool = _CapturingPool()
    pool.stored_text = "42"
    got = await StateManager(db_pool=pool).get("count", user=_user())
    assert got == "42"
    assert not isinstance(got, int)


@pytest.mark.asyncio
async def test_documented_rest_scalar_tradeoff_null_returns_string_not_none():
    # null is the most safety-critical case — we MUST preserve the
    # distinction between a legacy "null" string row and a missing
    # row (which returns None).
    pool = _CapturingPool()
    pool.stored_text = "null"
    got = await StateManager(db_pool=pool).get("nullable", user=_user())
    assert got == "null"
    assert got is not None


@pytest.mark.asyncio
async def test_state_manager_set_then_get_round_trips_scalar_via_envelope():
    # The supported path for typed scalar round-trips: write
    # through StateManager.set (which adds the envelope),
    # then read through StateManager.get.
    pool = _CapturingPool()
    writer = StateManager(db_pool=pool)
    user = _user()
    await writer.set("flag", True, user=user)
    cold = await StateManager(db_pool=pool).get("flag", user=user)
    assert cold is True
    assert isinstance(cold, bool)


@pytest.mark.asyncio
async def test_rest_object_with_legacy_sentinel_key_is_not_unwrapped():
    # The envelope is now a TEXT prefix (MNEMOS_SM:v1:), not a
    # JSON sentinel key, so a REST object that contains the OLD
    # sentinel key as data MUST round-trip unchanged. (Belt-and-
    # suspenders: also true for any singleton object — see
    # test_singleton_rest_object_round_trips_unchanged.)
    pool = _CapturingPool()
    pool.stored_text = '{"__mnemos_sm_v1__": 1, "keep": 2}'
    got = await StateManager(db_pool=pool).get("k", user=_user())
    assert got == {"__mnemos_sm_v1__": 1, "keep": 2}
    assert isinstance(got, dict)


@pytest.mark.asyncio
async def test_singleton_rest_object_round_trips_unchanged():
    # Round-12 finding: the previous in-JSON sentinel approach
    # would unwrap a singleton object {"__mnemos_sm_v1__": 1} to
    # the bare value 1. The TEXT-prefix envelope cannot collide,
    # so a singleton object — including one that uses the legacy
    # sentinel key — must come back as the original object.
    pool = _CapturingPool()
    pool.stored_text = '{"__mnemos_sm_v1__": 1}'
    got = await StateManager(db_pool=pool).get("k", user=_user())
    assert got == {"__mnemos_sm_v1__": 1}
    assert isinstance(got, dict)


@pytest.mark.asyncio
async def test_legacy_unwrapped_json_object_decodes_to_dict():
    # Pre-v4.2.0a5 StateManager wrote json.dumps(value) without the
    # envelope; the v4.2.0a5 column-type migration cast existing
    # JSONB rows to TEXT. Calling get on such a row must hydrate
    # back to the Python dict callers like load_identity expect, NOT
    # leave it as a JSON-text string.
    pool = _CapturingPool()
    pool.stored_text = '{"name": "alice", "machine_id": "macbook"}'
    got = await StateManager(db_pool=pool).get("identity", user=_user())
    assert got == {"name": "alice", "machine_id": "macbook"}
    assert isinstance(got, dict)


@pytest.mark.asyncio
async def test_legacy_unwrapped_json_array_decodes_to_list():
    pool = _CapturingPool()
    pool.stored_text = '[1, 2, 3]'
    got = await StateManager(db_pool=pool).get("seq", user=_user())
    assert got == [1, 2, 3]
    assert isinstance(got, list)


@pytest.mark.asyncio
async def test_rest_api_written_object_round_trips_through_state_manager():
    # /v1/state PUT writes json.dumps(req.value) directly without
    # the StateManager envelope. A subsequent StateManager.get must
    # still hydrate that to a dict so embedded callers and HTTP
    # callers see the same persisted state.
    pool = _CapturingPool()
    pool.stored_text = json.dumps({"workspace": "main", "active": True})
    got = await StateManager(db_pool=pool).get("workspace", user=_user())
    assert got == {"workspace": "main", "active": True}


@pytest.mark.asyncio
async def test_legacy_opaque_text_42_does_not_become_int():
    # Round-8 finding: blanket json.loads of every TEXT value would
    # silently retype legacy "42" as int 42. The envelope marker
    # prevents this — only StateManager-written rows decode.
    pool = _CapturingPool()
    pool.stored_text = "42"
    got = await StateManager(db_pool=pool).get("k", user=_user())
    assert got == "42"
    assert isinstance(got, str)


@pytest.mark.asyncio
async def test_legacy_opaque_text_true_does_not_become_bool():
    pool = _CapturingPool()
    pool.stored_text = "true"
    got = await StateManager(db_pool=pool).get("k", user=_user())
    assert got == "true"
    assert isinstance(got, str)


@pytest.mark.asyncio
async def test_legacy_opaque_text_null_stays_string_not_none():
    # Critical: "null" must NOT be indistinguishable from a missing
    # row. A missing row returns None; a legacy opaque "null" returns
    # the literal string "null".
    pool = _CapturingPool()
    pool.stored_text = "null"
    got = await StateManager(db_pool=pool).get("k", user=_user())
    assert got == "null"
    assert got is not None


@pytest.mark.asyncio
async def test_per_key_lock_attribute_exists():
    # Round-8 finding: get-during-set race could let a stale DB read
    # overwrite a fresh set's cache entry. The fix is per-(owner,
    # namespace, key) asyncio.Lock around get/set/delete. We assert
    # the lock attribute is present and that subsequent lookups for
    # the same key reuse the same Lock instance (so concurrent
    # callers actually serialize, rather than each acquiring its
    # own).
    import asyncio as _asyncio

    mgr = StateManager(db_pool=None)
    cache_key = ("alice", "ns", "k")
    lock_a = mgr._lock_for(cache_key)
    lock_b = mgr._lock_for(cache_key)
    assert isinstance(lock_a, _asyncio.Lock)
    assert lock_a is lock_b


@pytest.mark.asyncio
async def test_back_to_back_sets_last_writer_wins():
    # Sequential proof of the lock semantics: two sets land in
    # order, the second's value is what get returns.
    pool = _CapturingPool()
    mgr = StateManager(db_pool=pool)
    user = _user()
    await mgr.set("k", "first", user=user)
    await mgr.set("k", "second", user=user)
    assert await mgr.get("k", user=user) == "second"
    # And a fresh manager (cold cache) reads the same value.
    assert await StateManager(db_pool=pool).get("k", user=user) == "second"


@pytest.mark.asyncio
async def test_cold_cache_get_falls_back_to_raw_for_legacy_non_json_row():
    # Legacy rows from before the v4.2.0a5 TEXT migration may not be
    # JSON-shaped. The decode helper must NOT crash; the call returns
    # the raw bytes so callers can still see something.
    pool = _CapturingPool()
    pool.stored_text = "legacy opaque text without json wrap"

    reader = StateManager(db_pool=pool)
    user = _user()
    got = await reader.get("legacy", user=user)
    assert got == "legacy opaque text without json wrap"
