"""Federation sync engine — pull memories from remote MNEMOS peers.

Pull model: each peer is a remote instance; we periodically fetch their
`/v1/federation/feed` endpoint with a Bearer token they issued us. Memories
are stored locally with id = `fed:{peer_name}:{remote_id}` and
`federation_source = peer_name`, dedupable on re-pull via the id + updated
timestamp.

Peers are configured via admin endpoints (api/handlers/federation.py). A
lifespan-owned worker iterates enabled peers on their individual sync
intervals.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import asyncpg
import httpx

logger = logging.getLogger(__name__)

FEDERATION_HTTP_TIMEOUT = 30.0
FEDERATION_BATCH_LIMIT = 100
FEDERATION_ID_PREFIX = "fed:"
FEDERATION_CURSOR_LOWER_ID = ""
# Per-field size caps for incoming peer payloads. Hostile peers can otherwise
# fill disk by pushing 50MB blobs; these caps bound a single memory to ~1.5MB.
FEDERATION_MAX_CONTENT = 1_000_000  # 1 MB per content body
FEDERATION_MAX_METADATA = 64 * 1024  # 64 KB metadata json
FEDERATION_MAX_NAME = 256            # category/subcategory/namespace length


class FederationFeedCursor(NamedTuple):
    updated: datetime
    memory_id: str


def _cursor_timestamp_for_wire(updated: datetime) -> str:
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    else:
        updated = updated.astimezone(timezone.utc)
    return updated.isoformat().replace("+00:00", "Z")


def _cursor_timestamp_for_db(updated: datetime) -> datetime:
    if updated.tzinfo is not None:
        updated = updated.astimezone(timezone.utc)
    return updated.replace(tzinfo=None)


def _parse_cursor_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _encode_feed_cursor(updated: datetime, memory_id: str) -> str:
    payload = {
        "updated": _cursor_timestamp_for_wire(updated),
        "id": memory_id,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_feed_cursor(raw: str) -> FederationFeedCursor:
    """Decode a compound federation cursor."""
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        updated_raw = payload.get("updated")
        if not isinstance(updated_raw, str):
            raise ValueError("cursor payload missing updated")
        updated = _parse_cursor_timestamp(updated_raw)
        memory_id = payload.get("id")
        if not isinstance(memory_id, str):
            raise ValueError("cursor payload missing id")
        return FederationFeedCursor(updated=updated, memory_id=memory_id)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("invalid federation cursor")


def _cap(value, limit: int):
    """Truncate strings above `limit`. Pass-through for None/non-string."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit]
    return value


# ── Pull + store ─────────────────────────────────────────────────────────────


class FederationSchemaError(Exception):
    """Schema-compat preflight aborted the sync.

    Subclasses carry the kind of failure so the API surface can map
    to the right HTTP status (vs collapsing every sync_peer ValueError
    to 404 — Codex review-round-3 finding #2).
    """


class FederationSchemaIncompatible(FederationSchemaError):
    """Confirmed mismatch: peer responded but schema_signature or
    migrations_fingerprint differs from local. → HTTP 409."""


class FederationSchemaUnverifiable(FederationSchemaError):
    """Peer responded with a definitive 4xx (no /schema endpoint, bad
    auth, etc.) — peer is durably non-v3.4-compatible. → HTTP 409."""


class FederationSchemaTransient(FederationSchemaError):
    """Could not reach peer's /schema endpoint (network error, timeout,
    5xx). Sync should NOT consume the full sync_interval_secs — the
    next worker tick can retry. → HTTP 503."""


async def _check_peer_schema(
    base_url: str,
    auth_token: str,
    name: str,
) -> Dict[str, Any]:
    """GET peer's /v1/federation/schema.

    Returns a dict — never raises. Shape:
        {"ok": True,  "mnemos_version": str, "schema_signature": str,
         "migrations_fingerprint": str|None}
        {"ok": False, "transient": bool, "reason": str}

    `ok=False` + `transient=False` means the peer responded but is
    durably incompatible (4xx, missing fields). Strict mode MUST
    treat as a hard fail (Codex review-round-1 finding #1).

    `ok=False` + `transient=True` means we could not reach the peer
    (network error, timeout, 5xx). Strict mode should retry on the
    next worker tick rather than burning the full sync_interval_secs
    (Codex review-round-3 finding #1).
    """
    import httpx
    url = f"{base_url.rstrip('/')}/v1/federation/schema"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {auth_token}"}
            )
            if resp.status_code >= 500:
                # 5xx — transient infra failure on the peer.
                logger.warning(
                    "federation: peer %s /schema returned %d (transient)",
                    name, resp.status_code,
                )
                return {
                    "ok": False, "transient": True,
                    "reason": f"http {resp.status_code}",
                }
            if resp.status_code != 200:
                # 4xx — durable: peer doesn't speak the protocol or
                # rejected the auth. Pre-v3.4 peers land here too.
                logger.info(
                    "federation: peer %s /schema returned %d — "
                    "peer may pre-date v3.4 federation_compat",
                    name, resp.status_code,
                )
                return {
                    "ok": False, "transient": False,
                    "reason": f"http {resp.status_code}",
                }
            try:
                data = resp.json()
            except Exception as parse_err:
                # 200 but unparseable JSON — durable shape problem.
                return {
                    "ok": False, "transient": False,
                    "reason": f"unparseable schema response: {parse_err}",
                }
            mnemos_version = data.get("mnemos_version")
            schema_signature = data.get("schema_signature")
            if not mnemos_version or not schema_signature:
                return {
                    "ok": False, "transient": False,
                    "reason": "missing mnemos_version or schema_signature",
                }
            return {
                "ok": True,
                "mnemos_version": mnemos_version,
                "schema_signature": schema_signature,
                # Optional — older v3.4 builds may not return it; treat
                # as None and skip fingerprint comparison if absent.
                "migrations_fingerprint": data.get("migrations_fingerprint"),
            }
    # Codex review-round-4 finding #2 — expand transient envelope to
    # include the rest of httpx's retryable transport errors. Keep
    # local/config failures (InvalidURL, UnsupportedProtocol,
    # LocalProtocolError) durable so a misconfigured peer doesn't
    # spin forever on transient retries.
    except (httpx.ConnectError, httpx.ConnectTimeout,
            httpx.ReadTimeout, httpx.WriteTimeout,
            httpx.PoolTimeout, httpx.NetworkError,
            httpx.RemoteProtocolError, httpx.ProxyError) as e:
        logger.warning(
            "federation: peer %s /schema fetch failed (transient): %s",
            name, e,
        )
        return {
            "ok": False, "transient": True,
            "reason": f"{type(e).__name__}: {e}",
        }
    except (httpx.InvalidURL, httpx.UnsupportedProtocol,
            httpx.LocalProtocolError) as e:
        logger.warning(
            "federation: peer %s /schema config error: %s",
            name, e,
        )
        return {
            "ok": False, "transient": False,
            "reason": f"{type(e).__name__}: {e}",
        }
    except Exception as e:
        # Unrecognized — record but treat as durable so transient
        # backoff doesn't loop forever on a programming error.
        logger.warning(
            "federation: peer %s /schema fetch failed: %s",
            name, e,
        )
        return {
            "ok": False, "transient": False,
            "reason": f"{type(e).__name__}: {e}",
        }


_MIGRATIONS_FINGERPRINT_CACHE: Optional[str] = None


def _local_migrations_fingerprint() -> str:
    """Deterministic SHA256-prefix over (filename, content) of every
    migration in the deployed source tree.

    Codex review-round-1 finding #3 + round-7 finding #1: hashing
    filenames alone misses content drift — a downstream fork could
    edit the SQL inside an existing filename without changing the
    name. Hashing filename + file content catches that case. We
    cache the result at module load (migrations are immutable in a
    deployed container; recomputing on every /schema GET would burn
    disk I/O for no signal change).

    Limitations (deliberately deferred to V3_5_CHARTER):
      - We hash *deployed* migration files, not migrations *applied*
        to the running database. A migration that failed at apply-
        time still contributes to the fingerprint. Closing this gap
        means querying information_schema (or a migration ledger
        table) at /schema-serving time, which is more expensive and
        scoped for the "core fields + extensions" contract work.
      - The hash includes only db/migrations*.sql — handler-level
        contract changes (new endpoints, payload shape changes) are
        not captured here; mnemos_version + schema_signature carry
        that signal.
    """
    global _MIGRATIONS_FINGERPRINT_CACHE
    if _MIGRATIONS_FINGERPRINT_CACHE is not None:
        return _MIGRATIONS_FINGERPRINT_CACHE
    import hashlib
    from pathlib import Path
    db_dir = Path(__file__).parent.parent / "db"
    if not db_dir.is_dir():
        _MIGRATIONS_FINGERPRINT_CACHE = ""
        return ""
    h = hashlib.sha256()
    for p in sorted(db_dir.glob("migrations*.sql")):
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            # Permission/IO error reading a migration file — record
            # the name + a sentinel so the result still differentiates
            # this deployment from one where the file is readable.
            h.update(b"<unreadable>")
        h.update(b"\0\0")
    _MIGRATIONS_FINGERPRINT_CACHE = h.hexdigest()[:16]
    return _MIGRATIONS_FINGERPRINT_CACHE


async def sync_peer(
    pool: asyncpg.Pool,
    peer_id: str,
) -> Tuple[int, int, int]:
    """Run a full sync against one peer. Returns (pulled, new, updated).

    Pre-flight: query peer's /v1/federation/schema and compare
    schema_signature (major.minor) against ours. If mismatched and
    peer.compat_mode == 'strict', abort the sync with a clear error.
    Operators must explicitly set compat_mode='permissive' on a peer
    to allow cross-version sync.
    """
    async with pool.acquire() as conn:
        peer = await conn.fetchrow(
            """
            SELECT id::text, name, base_url, auth_token, namespace_filter,
                   category_filter, enabled, last_sync_cursor,
                   compat_mode
            FROM federation_peers WHERE id = $1::uuid
            """,
            peer_id,
        )
    if not peer:
        raise ValueError(f"peer {peer_id} not found")
    if not peer["enabled"]:
        logger.info("federation: peer %s disabled — skipping", peer["name"])
        return 0, 0, 0

    # Schema-compatibility pre-flight (added in v3.4 federation_compat).
    # See db/migrations_v3_4_federation_compat.sql for column meaning.
    from _version import __version__ as _local_v
    _local_parts = _local_v.split(".")
    local_signature = (
        f"{_local_parts[0]}.{_local_parts[1]}"
        if len(_local_parts) >= 2 else _local_v
    )
    local_fingerprint = _local_migrations_fingerprint()
    schema_resp = await _check_peer_schema(
        peer["base_url"], peer["auth_token"], peer["name"],
    )

    schema_abort_reason: Optional[str] = None
    schema_abort_kind: Optional[str] = None  # 'incompat'|'unverifiable'|'transient'
    peer_version: Optional[str] = None
    if schema_resp["ok"]:
        peer_version = schema_resp["mnemos_version"]
        peer_signature = schema_resp["schema_signature"]
        peer_fingerprint = schema_resp.get("migrations_fingerprint")
        sig_match = (peer_signature == local_signature)
        if not sig_match:
            schema_abort_reason = (
                f"schema mismatch: peer={peer_signature} ({peer_version}) "
                f"local={local_signature} ({_local_v})"
            )
            schema_abort_kind = "incompat"
        elif local_fingerprint == "":
            # We can't compute our own fingerprint (e.g. test rig
            # without a db/ directory). Falling back to signature-only
            # is the only option — accept.
            pass
        elif peer_fingerprint is None:
            # Codex review-round-6 finding #1 — peer at same major.minor
            # but doesn't expose migrations_fingerprint. Treat as
            # unverifiable in strict (peer might be a forked v3.4 with
            # extra/missing migrations). Operator can flip to permissive
            # if they trust the peer.
            schema_abort_reason = (
                f"peer {peer_version} matches signature {local_signature} "
                f"but does not expose migrations_fingerprint — cannot "
                f"verify same-minor schema drift"
            )
            schema_abort_kind = "unverifiable"
        elif peer_fingerprint != local_fingerprint:
            schema_abort_reason = (
                f"migrations fingerprint mismatch within {local_signature}: "
                f"peer={peer_fingerprint} local={local_fingerprint} "
                f"(peer={peer_version} local={_local_v})"
            )
            schema_abort_kind = "incompat"
    else:
        # Codex finding #1 (round 1) + #1 (round 3) — distinguish
        # transient (network/timeout/5xx) from durable (4xx, parse).
        # Both fail strict, but transient should NOT burn the full
        # sync_interval_secs — see strict-abort branch below.
        schema_abort_reason = (
            f"schema unverifiable ({schema_resp['reason']})"
        )
        schema_abort_kind = (
            "transient" if schema_resp.get("transient") else "unverifiable"
        )
    if schema_abort_reason is not None and peer["compat_mode"] == "strict":
        # Codex review-round-2 finding #2 — schema-metadata update,
        # sync_log row, and peer last_sync_at advance MUST commit as a
        # single transaction. A crash between split connections would
        # leave the peer with last_schema_check_at fresh but no log
        # row and no last_sync_at advance, putting the worker right
        # back into a tight retry loop on its next 60s scan.
        #
        # Codex review-round-3 finding #1 — transient probe failures
        # (network/timeout/5xx) should NOT burn the full
        # sync_interval_secs. We still record the failure in the log
        # and update peer metadata, but skip the last_sync_at advance
        # so the next 60s worker tick can re-attempt. Durable failures
        # (incompat, 4xx, parse) advance last_sync_at as normal.
        is_transient = (schema_abort_kind == "transient")
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE federation_peers
                    SET peer_mnemos_version = $2, last_schema_check_at = NOW()
                    WHERE id = $1::uuid
                    """,
                    peer_id, peer_version,
                )
                log_id = await conn.fetchval(
                    """
                    INSERT INTO federation_sync_log (peer_id, cursor_before)
                    VALUES ($1::uuid, $2) RETURNING id
                    """,
                    peer_id, peer["last_sync_cursor"],
                )
                await conn.execute(
                    """
                    UPDATE federation_sync_log
                    SET finished_at = NOW(),
                        memories_pulled = 0,
                        memories_new = 0,
                        memories_updated = 0,
                        error = $2,
                        cursor_after = $3
                    WHERE id = $1::uuid
                    """,
                    log_id, schema_abort_reason, peer["last_sync_cursor"],
                )
                if is_transient:
                    # Codex review-round-4 finding #1 — advance
                    # last_sync_at to "due in 60s" rather than not
                    # advancing at all. Otherwise a peer with a
                    # persistent transport flake stays at the front of
                    # the worker's LIMIT-10 ORDER BY last_sync_at queue
                    # and starves other due peers. Setting last_sync_at
                    # = NOW() - sync_interval + 60s makes the peer due
                    # again in ~60s but pushes it to the back of the
                    # queue so siblings get a turn.
                    await conn.execute(
                        """
                        UPDATE federation_peers
                        SET last_sync_at = NOW()
                                          - (sync_interval_secs || ' seconds')::interval
                                          + INTERVAL '60 seconds',
                            last_error = $2,
                            last_error_at = NOW()
                        WHERE id = $1::uuid
                        """,
                        peer_id, schema_abort_reason,
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE federation_peers
                        SET last_sync_at = NOW(),
                            last_error = $2,
                            last_error_at = NOW()
                        WHERE id = $1::uuid
                        """,
                        peer_id, schema_abort_reason,
                    )
        logger.error(
            "federation: peer %s — strict abort (%s): %s",
            peer["name"], schema_abort_kind, schema_abort_reason,
        )
        msg = (
            f"federation peer {peer['name']}: {schema_abort_reason}. "
            f"Set compat_mode='permissive' on the peer to allow "
            f"cross-version sync."
        )
        # Codex review-round-3 finding #2 — typed exceptions so the
        # API surface can map to the right HTTP status (was: every
        # ValueError → 404 "peer not found").
        if schema_abort_kind == "incompat":
            raise FederationSchemaIncompatible(msg)
        if schema_abort_kind == "transient":
            raise FederationSchemaTransient(msg)
        raise FederationSchemaUnverifiable(msg)

    # Non-strict-abort paths: still record what we learned about the
    # peer so operators have visibility into "last seen version X".
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE federation_peers
            SET peer_mnemos_version = $2, last_schema_check_at = NOW()
            WHERE id = $1::uuid
            """,
            peer_id, peer_version,
        )

    if schema_abort_reason is not None:
        # compat_mode == 'permissive' falls through to here.
        logger.warning(
            "federation: peer %s — permissive, proceeding despite: %s",
            peer["name"], schema_abort_reason,
        )
    else:
        logger.debug(
            "federation: peer %s schema-aligned at %s",
            peer["name"], local_signature,
        )

    cursor_before = peer["last_sync_cursor"]

    async with pool.acquire() as conn:
        log_id = await conn.fetchval(
            """
            INSERT INTO federation_sync_log (peer_id, cursor_before)
            VALUES ($1::uuid, $2) RETURNING id
            """,
            peer_id, cursor_before,
        )

    total_pulled = 0
    total_new = 0
    total_updated = 0
    cursor_request: Optional[datetime | FederationFeedCursor] = cursor_before
    cursor_persisted = cursor_before
    err: Optional[str] = None

    try:
        while True:
            batch, next_cursor, has_more = await _pull_batch(
                peer["base_url"], peer["auth_token"], cursor_request,
                peer["namespace_filter"], peer["category_filter"],
            )
            if not batch:
                break
            async with pool.acquire() as conn:
                new_n, upd_n = await _store_memories(conn, peer["name"], batch)
            total_pulled += len(batch)
            total_new += new_n
            total_updated += upd_n
            if next_cursor is not None:
                cursor_request = next_cursor
                cursor_persisted = next_cursor.updated
            if not has_more:
                break
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        logger.exception("federation: pull from %s failed", peer["name"])

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE federation_sync_log
            SET finished_at = NOW(),
                memories_pulled = $2,
                memories_new = $3,
                memories_updated = $4,
                error = $5,
                cursor_after = $6
            WHERE id = $1::uuid
            """,
            log_id, total_pulled, total_new, total_updated, err, cursor_persisted,
        )
        if err:
            await conn.execute(
                """
                UPDATE federation_peers
                SET last_sync_at = NOW(), last_error = $2, last_error_at = NOW()
                WHERE id = $1::uuid
                """,
                peer_id, err,
            )
        else:
            await conn.execute(
                """
                UPDATE federation_peers
                SET last_sync_at = NOW(),
                    last_sync_cursor = $2,
                    last_error = NULL,
                    last_error_at = NULL,
                    total_pulled = total_pulled + $3
                WHERE id = $1::uuid
                """,
                peer_id, cursor_persisted, total_pulled,
            )

    logger.info(
        "federation: peer=%s pulled=%d new=%d updated=%d cursor=%s",
        peer["name"], total_pulled, total_new, total_updated, cursor_persisted,
    )
    return total_pulled, total_new, total_updated


async def _pull_batch(
    base_url: str,
    auth_token: str,
    since: Optional[datetime | FederationFeedCursor],
    namespace_filter: Optional[List[str]],
    category_filter: Optional[List[str]],
) -> Tuple[List[Dict[str, Any]], Optional[FederationFeedCursor], bool]:
    """HTTP GET one batch. Returns (memories, next_cursor, has_more)."""
    url = base_url.rstrip("/") + "/v1/federation/feed"
    params: Dict[str, Any] = {"limit": FEDERATION_BATCH_LIMIT}
    if since is not None:
        if isinstance(since, FederationFeedCursor):
            params["since"] = _encode_feed_cursor(since.updated, since.memory_id)
        else:
            params["since"] = _encode_feed_cursor(since, FEDERATION_CURSOR_LOWER_ID)
    if namespace_filter:
        params["namespace"] = ",".join(namespace_filter)
    if category_filter:
        params["category"] = ",".join(category_filter)

    headers = {"Authorization": f"Bearer {auth_token}"}

    async with httpx.AsyncClient(timeout=FEDERATION_HTTP_TIMEOUT) as client:
        r = await client.get(url, params=params, headers=headers)
        if r.status_code == 401:
            raise RuntimeError("federation auth token rejected (401)")
        if r.status_code == 403:
            raise RuntimeError("federation auth insufficient role (403)")
        r.raise_for_status()
        body = r.json()

    memories = body.get("memories", []) or []
    next_cursor_raw = body.get("next_cursor")
    next_cursor = _decode_feed_cursor(next_cursor_raw) if next_cursor_raw else None
    has_more = bool(body.get("has_more"))
    return memories, next_cursor, has_more


async def _store_memories(
    conn: asyncpg.Connection,
    peer_name: str,
    memories: List[Dict[str, Any]],
) -> Tuple[int, int]:
    """Upsert a batch. Returns (newly_inserted, updated_existing)."""
    new_n = 0
    upd_n = 0
    for mem in memories:
        remote_id = mem.get("id")
        if not remote_id or not isinstance(remote_id, str):
            continue
        # Cap inbound strings. A hostile peer otherwise fills the disk.
        content = _cap(mem.get("content", ""), FEDERATION_MAX_CONTENT)
        verbatim = _cap(
            mem.get("verbatim_content") or mem.get("content", ""),
            FEDERATION_MAX_CONTENT,
        )
        category = _cap(mem.get("category", "federation"), FEDERATION_MAX_NAME)
        subcategory = _cap(mem.get("subcategory"), FEDERATION_MAX_NAME)
        namespace = _cap(mem.get("namespace", "default"), FEDERATION_MAX_NAME)
        local_id = f"{FEDERATION_ID_PREFIX}{peer_name}:{remote_id}"
        remote_updated_raw = mem.get("updated") or mem.get("created")
        if remote_updated_raw:
            try:
                remote_updated = datetime.fromisoformat(
                    remote_updated_raw.replace("Z", "+00:00")
                )
            except ValueError:
                remote_updated = None
        else:
            remote_updated = None

        # Check existing
        existing = await conn.fetchrow(
            "SELECT federation_remote_updated FROM memories WHERE id = $1",
            local_id,
        )

        meta_raw = mem.get("metadata") or {}
        if isinstance(meta_raw, dict):
            meta_raw = {**meta_raw, "federation_remote_id": remote_id}
        else:
            meta_raw = {"federation_remote_id": remote_id}
        meta_json = json.dumps(meta_raw)
        if len(meta_json) > FEDERATION_MAX_METADATA:
            # Drop metadata if it's absurdly large; keep the remote_id pointer.
            meta_json = json.dumps({"federation_remote_id": remote_id,
                                    "_metadata_truncated": True})

        if existing is None:
            await conn.execute(
                """
                INSERT INTO memories
                  (id, content, category, subcategory, metadata, verbatim_content,
                   quality_rating, owner_id, namespace, permission_mode,
                   source_model, source_provider, source_session, source_agent,
                   federation_source, federation_remote_updated, created, updated)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, 'federation', $8, 644,
                        $9, $10, $11, $12, $13, $14::timestamptz, NOW(),
                        ($14::timestamptz AT TIME ZONE 'UTC'))
                """,
                local_id,
                content,
                category,
                subcategory,
                meta_json,
                verbatim,
                mem.get("quality_rating") or 75,
                namespace,
                mem.get("source_model"),
                mem.get("source_provider"),
                mem.get("source_session"),
                mem.get("source_agent"),
                peer_name,
                remote_updated,
            )
            new_n += 1
        else:
            # Only update if the remote is newer.
            if (
                existing["federation_remote_updated"] is None
                or (remote_updated and remote_updated > existing["federation_remote_updated"])
            ):
                await conn.execute(
                    """
                    UPDATE memories SET
                      content = $2, category = $3, subcategory = $4,
                      metadata = $5::jsonb, verbatim_content = $6,
                      quality_rating = $7, namespace = $8,
                      federation_remote_updated = $9::timestamptz,
                      updated = ($9::timestamptz AT TIME ZONE 'UTC')
                    WHERE id = $1
                    """,
                    local_id,
                    content,
                    category,
                    subcategory,
                    meta_json,
                    verbatim,
                    mem.get("quality_rating") or 75,
                    namespace,
                    remote_updated,
                )
                upd_n += 1

    return new_n, upd_n


# ── Background worker ────────────────────────────────────────────────────────


async def federation_worker_loop(pool: asyncpg.Pool) -> None:
    """Background loop: iterate enabled peers, sync those whose interval has elapsed.

    Started from the FastAPI lifespan. Cancels cleanly on shutdown.
    """
    import asyncio

    logger.info("federation worker started")
    while True:
        try:
            await asyncio.sleep(60)  # check every minute
            async with pool.acquire() as conn:
                # Codex review-round-5 — order by computed next-due
                # time (last_sync_at + sync_interval), not last_sync_at
                # alone. Heterogeneous sync_interval_secs values mean a
                # long-interval peer's last_sync_at can be hours in the
                # past while still being LESS overdue than a short-
                # interval peer that just became due. The previous
                # `ORDER BY COALESCE(last_sync_at, epoch)` would have
                # let 10 long-interval transient-failing peers starve
                # short-interval healthy peers every 60s tick.
                due = await conn.fetch(
                    """
                    SELECT id::text, name, sync_interval_secs, last_sync_at
                    FROM federation_peers
                    WHERE enabled
                      AND (last_sync_at IS NULL
                           OR last_sync_at + (sync_interval_secs || ' seconds')::interval <= NOW())
                    ORDER BY COALESCE(
                        last_sync_at + (sync_interval_secs || ' seconds')::interval,
                        'epoch'::timestamptz
                    )
                    LIMIT 10
                    """
                )
            for p in due:
                try:
                    await sync_peer(pool, p["id"])
                except Exception:
                    logger.exception("federation: sync failed for peer %s", p["name"])
        except asyncio.CancelledError:
            logger.info("federation worker cancelled")
            raise
        except Exception:  # pragma: no cover
            logger.exception("federation worker iteration failed")
