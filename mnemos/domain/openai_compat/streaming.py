import json
import logging
from collections.abc import AsyncIterator
from typing import Any, Callable, Dict, List, Optional

from mnemos.domain.graeae.engine import ProviderStreamError

from .providers import OpenAICompatError, _prepare_provider_route
from .schemas import ChatCompletionDelta, ChatCompletionStreamChoice, ChatCompletionStreamResponse

logger = logging.getLogger(__name__)


def _stream_event(data: Dict[str, Any]) -> str:
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n"


def _stream_error_event(message: str, error_type: str = "provider_stream_error") -> str:
    return _stream_event({"error": {"message": message, "type": error_type}})


def _stream_chunk_event(
    *,
    stream_id: str,
    created: int,
    model: str,
    index: int,
    delta: ChatCompletionDelta,
    finish_reason: Optional[str] = None,
) -> str:
    chunk = ChatCompletionStreamResponse(
        id=stream_id,
        created=created,
        model=model,
        choices=[
            ChatCompletionStreamChoice(
                index=index,
                delta=delta,
                finish_reason=finish_reason,
            )
        ],
    )
    return _stream_event(chunk.model_dump(exclude_none=True))


def _stream_events_for_provider_delta(
    *,
    delta: Dict[str, Any],
    stream_id: str,
    created: int,
    model: str,
    started_indexes: set[int],
    finished_indexes: set[int],
) -> List[str]:
    index = int(delta.get("index", 0))
    events: List[str] = []

    if index not in started_indexes:
        started_indexes.add(index)
        events.append(
            _stream_chunk_event(
                stream_id=stream_id,
                created=created,
                model=model,
                index=index,
                delta=ChatCompletionDelta(role=delta.get("role") or "assistant"),
            )
        )

    has_delta_payload = delta.get("content") is not None or delta.get("tool_calls") is not None
    if has_delta_payload:
        events.append(
            _stream_chunk_event(
                stream_id=stream_id,
                created=created,
                model=model,
                index=index,
                delta=ChatCompletionDelta(
                    content=delta.get("content"),
                    tool_calls=delta.get("tool_calls"),
                ),
            )
        )

    finish_reason = delta.get("finish_reason")
    if finish_reason is not None and index not in finished_indexes:
        finished_indexes.add(index)
        events.append(
            _stream_chunk_event(
                stream_id=stream_id,
                created=created,
                model=model,
                index=index,
                delta=ChatCompletionDelta(),
                finish_reason=finish_reason,
            )
        )

    return events


def _stream_preflight_exception(exc: Exception) -> OpenAICompatError:
    message = str(exc)
    status_code = getattr(exc, "status_code", 503)
    status_prefix = message.split(":", 1)[0].split()
    if len(status_prefix) == 2 and status_prefix[0] == "HTTP":
        try:
            upstream_status = int(status_prefix[1])
        except ValueError:
            upstream_status = 0
        if 400 <= upstream_status <= 599:
            status_code = upstream_status
    elif "rate-limited" in message:
        status_code = 429
    return OpenAICompatError(status_code=status_code, detail=f"Streaming request failed: {message}")


async def _route_to_provider_stream(
    model: str,
    messages: List[Dict[str, Any]],
    generation_params: Optional[Dict[str, Any]] = None,
    request_params: Optional[Dict[str, Any]] = None,
    user: Optional[Any] = None,
    *,
    resolve_provider: Optional[Callable[[str], Any]] = None,
    get_engine: Optional[Callable[[], Any]] = None,
) -> AsyncIterator[Dict[str, Any]]:
    graeae, provider, bare_model, prompt = await _prepare_provider_route(
        model=model,
        messages=messages,
        request_params=request_params,
        resolve_provider=resolve_provider,
        get_engine=get_engine,
    )
    try:
        async for chunk in graeae.route_stream(
            provider,
            bare_model,
            prompt,
            task_type="reasoning",
            timeout=30,
            generation_params=generation_params,
            request_params=request_params,
            messages=messages,
        ):
            yield chunk
    except Exception as e:
        logger.error("[MNEMOS] Streaming route to %s failed: %s", provider, e, exc_info=True)
        raise


async def stream_event_source(
    *,
    provider_stream: AsyncIterator[Dict[str, Any]],
    first_delta: Optional[Dict[str, Any]],
    stream_id: str,
    created: int,
    model: str,
) -> AsyncIterator[str]:
    started_indexes: set[int] = set()
    finished_indexes: set[int] = set()
    try:
        if first_delta is not None:
            for event in _stream_events_for_provider_delta(
                delta=first_delta,
                stream_id=stream_id,
                created=created,
                model=model,
                started_indexes=started_indexes,
                finished_indexes=finished_indexes,
            ):
                yield event

        async for delta in provider_stream:
            for event in _stream_events_for_provider_delta(
                delta=delta,
                stream_id=stream_id,
                created=created,
                model=model,
                started_indexes=started_indexes,
                finished_indexes=finished_indexes,
            ):
                yield event
    except Exception as e:
        logger.error("[MNEMOS] Streaming response failed after response start: %s", e, exc_info=True)
        error_type = e.error_type if isinstance(e, ProviderStreamError) else "provider_stream_error"
        yield _stream_error_event(str(e), error_type=error_type)
    finally:
        try:
            await provider_stream.aclose()
        except Exception as e:
            logger.debug("[MNEMOS] Streaming response cleanup failed: %s", e)
    yield "data: [DONE]\n\n"
