"""FastAPI shim for the OpenAI-compatible MNEMOS gateway."""

from collections.abc import AsyncIterator
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.domain.openai_compat import content, providers, router as domain_router, schemas, streaming

router = APIRouter(tags=["openai"])

_EXPORTS = {
    # Wire schemas
    "ContentBlock": (schemas, "ContentBlock"),
    "ChatContent": (schemas, "ChatContent"),
    "ToolFunction": (schemas, "ToolFunction"),
    "Tool": (schemas, "Tool"),
    "ChatMessage": (schemas, "ChatMessage"),
    "ChatCompletionRequest": (schemas, "ChatCompletionRequest"),
    "ChatCompletionStreamRequest": (schemas, "ChatCompletionStreamRequest"),
    "ChatCompletionResponseMessage": (schemas, "ChatCompletionResponseMessage"),
    "ChatCompletionChoice": (schemas, "ChatCompletionChoice"),
    "ChatCompletionResponse": (schemas, "ChatCompletionResponse"),
    "ChatCompletionDelta": (schemas, "ChatCompletionDelta"),
    "ChatCompletionStreamChoice": (schemas, "ChatCompletionStreamChoice"),
    "ChatCompletionStreamResponse": (schemas, "ChatCompletionStreamResponse"),
    "ModelInfo": (schemas, "ModelInfo"),
    "ModelsResponse": (schemas, "ModelsResponse"),
    # Legacy private helper imports retained during the split.
    "TASK_CAPABILITY_MAP": (providers, "TASK_CAPABILITY_MAP"),
    "MODEL_ALIASES": (providers, "MODEL_ALIASES"),
    "get_graeae_engine": (providers, "get_graeae_engine"),
    "_serialize_content": (content, "_serialize_content"),
    "_plain_value": (content, "_plain_value"),
    "_content_text": (content, "_content_text"),
    "_message_to_dict": (content, "_message_to_dict"),
    "_has_content_blocks": (content, "_has_content_blocks"),
    "_has_message_names": (content, "_has_message_names"),
    "_flatten_messages_for_prompt": (content, "_flatten_messages_for_prompt"),
    "_generation_params": (providers, "_generation_params"),
    "_request_params": (providers, "_request_params"),
    "_provider_supports_tools": (providers, "_provider_supports_tools"),
    "_provider_supports_response_format": (providers, "_provider_supports_response_format"),
    "_provider_supports_multimodal": (providers, "_provider_supports_multimodal"),
    "_provider_supports_stop": (providers, "_provider_supports_stop"),
    "_provider_supports_n": (providers, "_provider_supports_n"),
    "_provider_supports_penalties": (providers, "_provider_supports_penalties"),
    "_validate_anthropic_tool_choice": (providers, "_validate_anthropic_tool_choice"),
    "_validate_provider_roles": (providers, "_validate_provider_roles"),
    "_validate_provider_request": (providers, "_validate_provider_request"),
    "_fallback_provider_from_name": (providers, "_fallback_provider_from_name"),
    "_model_not_found_error": (providers, "_model_not_found_error"),
    "_strip_gateway_namespace": (providers, "_strip_gateway_namespace"),
    "_stream_event": (streaming, "_stream_event"),
    "_stream_error_event": (streaming, "_stream_error_event"),
    "_stream_preflight_exception": (streaming, "_stream_preflight_exception"),
    "_stream_events_for_provider_delta": (streaming, "_stream_events_for_provider_delta"),
    "_search_mnemos_context": (domain_router, "search_memory_context"),
    "_get_model_recommendation": (domain_router, "get_model_recommendation"),
}

__all__ = [
    "router",
    "list_models",
    "get_model",
    "chat_completions",
    "_prepare_provider_route",
    "_resolve_provider_for_model",
    "_route_to_provider",
    "_route_to_provider_response",
    "_route_to_provider_stream",
    *_EXPORTS,
]


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        module, attr = _EXPORTS[name]
        return getattr(module, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _compat_get_engine() -> Any:
    return globals().get("get_graeae_engine", providers.get_graeae_engine)()


async def _resolve_provider_for_model(model: str) -> Optional[str]:
    return await providers._resolve_provider_for_model(model)


def _compat_resolver() -> Any:
    return globals().get("_resolve_provider_for_model", _resolve_provider_for_model)


def _to_http_exception(exc: providers.OpenAICompatError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _parse_memory_injection_header(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized == "false":
        return False
    if normalized == "true":
        return True
    return None


def _memory_injection_enabled(
    request: schemas.ChatCompletionRequest,
    header_value: Optional[str],
) -> bool:
    header_decision = _parse_memory_injection_header(header_value)
    if header_decision is False:
        return False
    if request.mnemos_inject_memory is False:
        return False
    return True


def _response_with_mnemos_metadata(
    response: schemas.ChatCompletionResponse,
    *,
    memory_injected: bool,
) -> JSONResponse:
    body = response.model_dump(mode="json")
    body["mnemos_metadata"] = {"memory_injected": memory_injected}
    return JSONResponse(content=body)


async def _prepare_provider_route(
    model: str,
    messages: List[Dict[str, Any]],
    request_params: Optional[Dict[str, Any]] = None,
) -> tuple[Any, str, str, str]:
    try:
        return await providers._prepare_provider_route(
            model=model,
            messages=messages,
            request_params=request_params,
            resolve_provider=_compat_resolver(),
            get_engine=_compat_get_engine,
        )
    except providers.OpenAICompatError as exc:
        raise _to_http_exception(exc) from exc


async def _route_to_provider_response_for_domain(
    model: str,
    messages: List[Dict[str, Any]],
    generation_params: Optional[Dict[str, Any]] = None,
    request_params: Optional[Dict[str, Any]] = None,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    return await providers._route_to_provider_response(
        model=model,
        messages=messages,
        generation_params=generation_params,
        request_params=request_params,
        user=user,
        resolve_provider=_compat_resolver(),
        get_engine=_compat_get_engine,
    )


async def _route_to_provider_response(
    model: str,
    messages: List[Dict[str, Any]],
    generation_params: Optional[Dict[str, Any]] = None,
    request_params: Optional[Dict[str, Any]] = None,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    try:
        return await _route_to_provider_response_for_domain(
            model=model,
            messages=messages,
            generation_params=generation_params,
            request_params=request_params,
            user=user,
        )
    except providers.OpenAICompatError as exc:
        raise _to_http_exception(exc) from exc


def _route_to_provider_stream_for_domain(
    model: str,
    messages: List[Dict[str, Any]],
    generation_params: Optional[Dict[str, Any]] = None,
    request_params: Optional[Dict[str, Any]] = None,
    user: Optional[UserContext] = None,
) -> AsyncIterator[Dict[str, Any]]:
    return streaming._route_to_provider_stream(
        model=model,
        messages=messages,
        generation_params=generation_params,
        request_params=request_params,
        user=user,
        resolve_provider=_compat_resolver(),
        get_engine=_compat_get_engine,
    )


async def _route_to_provider_stream(
    model: str,
    messages: List[Dict[str, Any]],
    generation_params: Optional[Dict[str, Any]] = None,
    request_params: Optional[Dict[str, Any]] = None,
    user: Optional[UserContext] = None,
) -> AsyncIterator[Dict[str, Any]]:
    try:
        async for chunk in _route_to_provider_stream_for_domain(
            model=model,
            messages=messages,
            generation_params=generation_params,
            request_params=request_params,
            user=user,
        ):
            yield chunk
    except providers.OpenAICompatError as exc:
        raise _to_http_exception(exc) from exc


async def _route_to_provider(
    model: str,
    messages: List[Dict[str, Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
    user: UserContext,
    top_p: Optional[float] = None,
    request_params: Optional[Dict[str, Any]] = None,
) -> str:
    try:
        return await providers._route_to_provider(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            user=user,
            top_p=top_p,
            request_params=request_params,
            resolve_provider=_compat_resolver(),
            get_engine=_compat_get_engine,
        )
    except providers.OpenAICompatError as exc:
        raise _to_http_exception(exc) from exc


@router.get("/v1/models", response_model=schemas.ModelsResponse)
async def list_models(
    authorization: Optional[str] = Header(None),
    user: UserContext = Depends(get_current_user),
):
    try:
        return await domain_router.list_models(user=user)
    except providers.OpenAICompatError as exc:
        raise _to_http_exception(exc) from exc


@router.get("/v1/models/{model_id}")
async def get_model(
    model_id: str,
    authorization: Optional[str] = Header(None),
    user: UserContext = Depends(get_current_user),
):
    try:
        return await domain_router.get_model(model_id=model_id, user=user)
    except providers.OpenAICompatError as exc:
        raise _to_http_exception(exc) from exc


@router.post("/v1/chat/completions", response_model=schemas.ChatCompletionResponse)
async def chat_completions(
    request: schemas.ChatCompletionRequest,
    authorization: Optional[str] = Header(None),
    x_mnemos_inject_memory: Annotated[
        Optional[str],
        Header(alias="X-Mnemos-Inject-Memory"),
    ] = None,
    user: UserContext = Depends(get_current_user),
):
    memory_injected = _memory_injection_enabled(request, x_mnemos_inject_memory)
    try:
        response = await domain_router.chat_completion(
            request=request,
            user=user,
            inject_memory=memory_injected,
            search_context=globals().get(
                "_search_mnemos_context",
                domain_router.search_memory_context,
            ),
            get_model_recommendation=globals().get(
                "_get_model_recommendation",
                domain_router.get_model_recommendation,
            ),
            route_to_provider_response=_route_to_provider_response_for_domain,
            route_to_provider_stream=_route_to_provider_stream_for_domain,
        )
    except providers.OpenAICompatError as exc:
        if domain_router._is_openai_error_detail(exc.detail):
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        raise _to_http_exception(exc) from exc

    if isinstance(response, domain_router.StreamingChatCompletion):
        return StreamingResponse(response.events, media_type="text/event-stream")
    if x_mnemos_inject_memory is not None:
        return _response_with_mnemos_metadata(
            response,
            memory_injected=memory_injected,
        )
    return response
