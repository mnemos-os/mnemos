"""Webhook HTTP send pipeline and response-body audit capture."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Awaitable, Optional

import asyncpg
import httpx

from . import lease as webhook_lease
from . import types as webhook_types
from ._signing import _sign
from .types import _ClaimedDelivery, _DeliveryResult, _LeaseExpiredBeforeSend, _PostHeaderDeliveryResult

logger = logging.getLogger(__name__)


async def _attempt_delivery(
    delivery_id: str,
    *,
    pool: Optional[asyncpg.Pool] = None,
    claimed: Optional[_ClaimedDelivery] = None,
) -> bool:
    """Claim, send, and finalize one delivery without holding DB during I/O."""
    if pool is None:
        from mnemos.core.lifecycle import _pool as lifecycle_pool  # noqa: WPS433
        pool = lifecycle_pool
    if not pool:
        logger.warning("webhook dispatcher: no DB pool - skipping delivery %s", delivery_id)
        return False

    async with webhook_types._get_send_semaphore():
        if claimed is None:
            lease_token = str(uuid.uuid4())
            claimed = await webhook_lease._claim_delivery(pool, delivery_id, lease_token=lease_token)
        else:
            lease_token = claimed.lease_token
            if not await webhook_lease._guard_preclaimed_delivery_before_send(
                pool,
                claimed.delivery,
                lease_token,
            ):
                return False
        if not claimed:
            return False
        result = await _send_claimed_delivery(
            claimed.delivery,
            pre_claim_monotonic=claimed.pre_claim_monotonic,
        )
        from .finalize import _finalize_delivery
        return await _finalize_delivery(pool, claimed.delivery, lease_token, result)


async def _send_claimed_delivery(
    delivery: asyncpg.Record,
    *,
    pre_claim_monotonic: float,
) -> _DeliveryResult:
    """Perform DNS validation and HTTP POST for an already leased row."""
    remaining_seconds = webhook_lease._claim_remaining_send_window_seconds(
        delivery,
        pre_claim_monotonic=pre_claim_monotonic,
    )
    if remaining_seconds <= webhook_types.MIN_SEND_WINDOW_SECONDS:
        return _LeaseExpiredBeforeSend(
            succeeded=False,
            error=(
                "lease-expired-before-send: remaining lease send window "
                f"{remaining_seconds:.3f}s <= {webhook_types.MIN_SEND_WINDOW_SECONDS:.1f}s minimum"
            ),
        )

    try:
        return await _send_claimed_delivery_within_deadline(
            delivery,
            send_window_seconds=remaining_seconds,
        )
    except asyncio.TimeoutError:
        return _DeliveryResult(
            succeeded=False,
            error=(
                "send-timeout: DNS validation, HTTP send, and response headers "
                f"exceeded {remaining_seconds:.1f}s lease-anchored wall-clock deadline"
            ),
        )


async def _send_claimed_delivery_within_deadline(
    delivery: asyncpg.Record,
    *,
    send_window_seconds: float,
) -> _DeliveryResult:
    """Run the network send path; caller supplies the wall-clock deadline."""
    if not delivery:
        return _DeliveryResult(succeeded=False, error="delivery not found")
    if delivery["revoked"]:
        return _DeliveryResult(succeeded=False, error="subscription revoked")

    signature = _sign(delivery["secret"], delivery["payload"])
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "MNEMOS-Webhook/1.0",
        "Accept-Encoding": "identity",
        "X-MNEMOS-Event": delivery["event_type"],
        "X-MNEMOS-Signature": f"sha256={signature}",
        "X-MNEMOS-Delivery-ID": str(delivery["id"]),
        "X-MNEMOS-Subscription-ID": str(delivery["subscription_id"]),
        "X-MNEMOS-Attempt": str(delivery["attempt_num"]),
    }

    loop = asyncio.get_running_loop()
    header_deadline = loop.time() + send_window_seconds

    try:
        from mnemos.api.routes.webhooks import validate_webhook_url
        await asyncio.wait_for(
            validate_webhook_url(delivery["url"]),
            timeout=_remaining_timeout_seconds(header_deadline),
        )
    except asyncio.TimeoutError:
        raise
    except Exception as e:
        return _DeliveryResult(succeeded=False, error=f"url-rejected: {type(e).__name__}: {e}")

    client_cm = httpx.AsyncClient(timeout=webhook_types.DELIVERY_TIMEOUT, follow_redirects=False)
    client = None
    stream_cm = None
    try:
        client = await asyncio.wait_for(
            client_cm.__aenter__(),
            timeout=_remaining_timeout_seconds(header_deadline),
        )
        stream_cm = client.stream(
            "POST",
            delivery["url"],
            content=delivery["payload"].encode("utf-8"),
            headers=headers,
        )
        response = await asyncio.wait_for(
            stream_cm.__aenter__(),
            timeout=_remaining_timeout_seconds(header_deadline),
        )
        response_status = response.status_code
        succeeded = 200 <= response_status < 300
        return _PostHeaderDeliveryResult(
            succeeded=succeeded,
            response_status=response_status,
            delivery_id=delivery["id"],
            response=response,
            stream_cm=stream_cm,
            client_cm=client_cm,
        )
    except asyncio.TimeoutError:
        await _cleanup_unacknowledged_send_context(
            delivery_id=delivery["id"],
            stream_cm=stream_cm,
            client_cm=client_cm,
            client=client,
        )
        raise
    except httpx.HTTPError as e:
        await _cleanup_unacknowledged_send_context(
            delivery_id=delivery["id"],
            stream_cm=stream_cm,
            client_cm=client_cm,
            client=client,
        )
        return _DeliveryResult(succeeded=False, error=f"{type(e).__name__}: {e}")
    except Exception as e:  # pragma: no cover
        await _cleanup_unacknowledged_send_context(
            delivery_id=delivery["id"],
            stream_cm=stream_cm,
            client_cm=client_cm,
            client=client,
        )
        return _DeliveryResult(succeeded=False, error=f"{type(e).__name__}: {e}")


async def _cleanup_unacknowledged_send_context(
    *,
    delivery_id: Any,
    stream_cm: Any,
    client_cm: Any,
    client: Any,
) -> None:
    """Best-effort cleanup when the POST did not produce response headers."""
    if stream_cm is not None:
        await _run_pre_header_cleanup(
            stream_cm.__aexit__(None, None, None),
            delivery_id=delivery_id,
            cleanup_name="stream",
        )
    if client is not None:
        await _run_pre_header_cleanup(
            client_cm.__aexit__(None, None, None),
            delivery_id=delivery_id,
            cleanup_name="client",
        )


async def _run_pre_header_cleanup(
    cleanup: Awaitable[object],
    *,
    delivery_id: Any,
    cleanup_name: str,
) -> None:
    """Drain failed pre-header send resources without changing the delivery result."""
    try:
        await asyncio.wait_for(
            cleanup,
            timeout=webhook_types.WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "webhook delivery %s %s cleanup failed before response headers",
            delivery_id,
            cleanup_name,
            exc_info=True,
        )


async def _run_post_header_cleanup(
    cleanup: Awaitable[object],
    *,
    delivery_id: Any,
    cleanup_name: str,
    result: _DeliveryResult | None,
) -> None:
    """Bound post-header cleanup so finalization is not stuck behind __aexit__."""
    cleanup_task = asyncio.ensure_future(cleanup)
    try:
        await asyncio.wait_for(
            asyncio.shield(cleanup_task),
            timeout=webhook_types.WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        cleanup_task.cancel()
        cleanup_task.add_done_callback(
            lambda task: _consume_timed_out_cleanup_result(
                task,
                delivery_id,
                cleanup_name,
            )
        )
        if result is None:
            raise
        logger.warning(
            "webhook delivery %s %s cleanup timed out after %.1fs after response headers; "
            "delivery result preserved",
            delivery_id,
            cleanup_name,
            webhook_types.WEBHOOK_POST_HEADER_CLEANUP_TIMEOUT_SECONDS,
            exc_info=True,
        )
    except asyncio.CancelledError:
        cleanup_task.cancel()
        raise
    except Exception:
        if result is None:
            raise
        logger.warning(
            "webhook delivery %s %s cleanup failed after response headers; "
            "delivery result preserved",
            delivery_id,
            cleanup_name,
            exc_info=True,
        )


def _consume_timed_out_cleanup_result(
    task: asyncio.Future[object],
    delivery_id: Any,
    cleanup_name: str,
) -> None:
    """Drain a timed-out cleanup task if it finishes after finalization moved on."""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.debug(
            "webhook delivery %s %s cleanup finished with an error after timeout",
            delivery_id,
            cleanup_name,
            exc_info=True,
        )


def _remaining_timeout_seconds(deadline: float) -> float:
    """Return a small positive timeout if the header deadline is exhausted."""
    return max(0.001, deadline - asyncio.get_running_loop().time())


async def _capture_response_body_for_audit(
    response: httpx.Response,
    *,
    delivery_id: Any,
) -> Optional[str]:
    """Read response body for audit after the status-code result is finalized."""
    try:
        async with asyncio.timeout(webhook_types.WEBHOOK_RESPONSE_BODY_CAPTURE_TIMEOUT_SECONDS):
            return await _read_capped_response_body(response)
    except asyncio.TimeoutError:
        logger.warning(
            "webhook delivery %s response-body audit capture timed out after %.1fs; "
            "leaving response_body NULL",
            delivery_id,
            webhook_types.WEBHOOK_RESPONSE_BODY_CAPTURE_TIMEOUT_SECONDS,
            exc_info=True,
        )
        return None
    except httpx.HTTPError as e:
        return f"[body capture: {type(e).__name__}]"
    except Exception as e:  # pragma: no cover
        return f"[body capture: {type(e).__name__}]"


async def _read_capped_response_body(response: httpx.Response) -> str:
    """Read bounded raw response bytes; never transparently decompress."""
    headers = getattr(response, "headers", {})
    content_encoding = str(headers.get("content-encoding", "identity") or "identity").strip().lower()
    if content_encoding not in ("", "identity"):
        # Receivers may ignore Accept-Encoding: identity; retain only raw bytes
        # so a compressed response cannot inflate before the audit cap applies.
        raw = await _read_capped_raw_response_body(
            response,
            max_bytes=min(webhook_types.WEBHOOK_RESPONSE_BODY_MAX_BYTES, webhook_types.NON_IDENTITY_RESPONSE_BODY_PREVIEW_BYTES),
        )
        return _decode_capped_response_body(raw, webhook_types.WEBHOOK_RESPONSE_BODY_MAX_BYTES)

    raw = await _read_capped_raw_response_body(response, max_bytes=webhook_types.WEBHOOK_RESPONSE_BODY_MAX_BYTES)
    return _decode_capped_response_body(raw, webhook_types.WEBHOOK_RESPONSE_BODY_MAX_BYTES)


async def _read_capped_raw_response_body(response: httpx.Response, *, max_bytes: int) -> bytes:
    """Read at most max_bytes raw bytes from a streamed response."""
    remaining = max_bytes
    body = bytearray()
    async for chunk in response.aiter_raw():
        if not chunk:
            continue
        body.extend(chunk[:remaining])
        remaining -= min(len(chunk), remaining)
        if remaining <= 0:
            break
    return bytes(body)


def _decode_capped_response_body(raw: bytes, max_bytes: int) -> str:
    """Decode for TEXT storage while preserving the configured UTF-8 byte cap."""
    text = raw.decode("utf-8", errors="replace")
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    used = 0
    out: list[str] = []
    for char in text:
        char_size = len(char.encode("utf-8"))
        if used + char_size > max_bytes:
            break
        out.append(char)
        used += char_size
    return "".join(out)
