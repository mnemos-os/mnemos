import importlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol

from mnemos.core.config import get_registered_task_classifier, get_settings
from mnemos.db import openai_compat_repo

from .content import _content_text, _message_to_dict
from .providers import (
    MODEL_ALIASES,
    OpenAICompatError,
    _generation_params,
    _request_params,
    _route_to_provider_response,
)
from .schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionResponseMessage,
    ChatMessage,
    ModelInfo,
    ModelsResponse,
)
from .streaming import (
    _route_to_provider_stream,
    _stream_preflight_exception,
    stream_event_source,
)

logger = logging.getLogger(__name__)

_CODE_KEYWORDS = ("code", "function", "class", "def", "import", "syntax")
_ARCHITECTURE_KEYWORDS = ("arch", "design", "pattern", "structure", "system")
_DEFAULT_CLASSIFIER_WARNING = (
    "Default English-keyword classifier is opinionated and language-specific. "
    "To customize, set MNEMOS_TASK_CLASSIFIER_FACTORY."
)
_DEFAULT_CLASSIFIER_WARNED = False
_FACTORY_CLASSIFIER: Any | None = None
_FACTORY_CLASSIFIER_PATH: str | None = None


class TaskClassifier(Protocol):
    def classify(self, messages: List[ChatMessage]) -> str:
        ...


class KeywordTaskClassifier:
    """English-only exhaustive keyword classifier.

    This default classifier only checks the exact keyword lists in
    ``_CODE_KEYWORDS`` and ``_ARCHITECTURE_KEYWORDS`` against the last
    user message. It is intentionally not a language-agnostic heuristic:
    if no listed English substring matches, it returns ``reasoning``.
    """

    def classify(self, messages: List[ChatMessage]) -> str:
        last_msg = _last_user_message_text(messages)
        lower = last_msg.lower()
        if any(keyword in lower for keyword in _CODE_KEYWORDS):
            return "code_generation"
        if any(keyword in lower for keyword in _ARCHITECTURE_KEYWORDS):
            return "architecture_design"
        return "reasoning"


_DEFAULT_CLASSIFIER = KeywordTaskClassifier()


@dataclass
class StreamingChatCompletion:
    events: AsyncIterator[str]


_RESPONSE_MESSAGE_FIELDS = {
    "role",
    "content",
    "name",
    "tool_calls",
    "tool_call_id",
    "function_call",
    "refusal",
    "audio",
    "annotations",
}


def _response_message_data(message: Dict[str, Any]) -> Dict[str, Any]:
    for key in message:
        if key not in _RESPONSE_MESSAGE_FIELDS:
            raise OpenAICompatError(
                status_code=502,
                detail=(
                    f"provider returned unsupported response field {key}; "
                    "gateway cannot faithfully represent"
                ),
            )
    return dict(message)


def _provider_choices(response: Dict[str, Any]) -> List[ChatCompletionChoice]:
    raw_choices = response.get("choices") or []
    choices: List[ChatCompletionChoice] = []
    for i, choice in enumerate(raw_choices):
        message_data = _response_message_data(
            choice.get("message")
            or {
                "role": "assistant",
                "content": choice.get("text") or "",
            }
        )
        choices.append(
            ChatCompletionChoice(
                index=choice.get("index", i),
                message=ChatCompletionResponseMessage(**message_data),
                finish_reason=choice.get("finish_reason") or "stop",
            )
        )
    if choices:
        return choices
    return [
        ChatCompletionChoice(
            index=0,
            message=ChatCompletionResponseMessage(
                role="assistant",
                content=response.get("response_text", ""),
            ),
            finish_reason=response.get("finish_reason") or "stop",
        )
    ]


def _completion_text_for_usage(choices: List[ChatCompletionChoice]) -> str:
    return "\n".join(_content_text(choice.message.content) for choice in choices)


def _validate_request_messages(messages: List[ChatMessage]) -> None:
    for msg in messages:
        if msg.function_call is not None:
            raise OpenAICompatError(
                status_code=400,
                detail="message.function_call is deprecated; use tool_calls and tool messages instead",
            )


def _last_user_message_text(messages: List[ChatMessage]) -> str:
    for msg in reversed(messages):
        if msg.role == "user":
            return _content_text(msg.content)
    return ""


def _import_from_path(path: str) -> Any:
    module_path, sep, attr = path.partition(":")
    if not sep:
        module_path, _, attr = path.rpartition(".")
    if not module_path or not attr:
        raise ValueError(
            "MNEMOS_TASK_CLASSIFIER_FACTORY must be an import path like "
            "'package.module:factory'"
        )
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def _coerce_task_classifier(candidate: Any) -> TaskClassifier:
    if isinstance(candidate, type):
        candidate = candidate()
    elif not hasattr(candidate, "classify") and callable(candidate):
        candidate = candidate()
    if not hasattr(candidate, "classify"):
        raise TypeError("task classifier must expose classify(messages) -> str")
    return candidate


def _configured_task_classifier() -> TaskClassifier:
    global _DEFAULT_CLASSIFIER_WARNED, _FACTORY_CLASSIFIER, _FACTORY_CLASSIFIER_PATH

    registered = get_registered_task_classifier()
    if registered is not None:
        return _coerce_task_classifier(registered)

    factory_path = get_settings().runtime.task_classifier_factory.strip()
    if factory_path:
        if _FACTORY_CLASSIFIER is None or _FACTORY_CLASSIFIER_PATH != factory_path:
            _FACTORY_CLASSIFIER = _coerce_task_classifier(_import_from_path(factory_path))
            _FACTORY_CLASSIFIER_PATH = factory_path
        return _FACTORY_CLASSIFIER

    if not _DEFAULT_CLASSIFIER_WARNED:
        logger.warning(_DEFAULT_CLASSIFIER_WARNING)
        _DEFAULT_CLASSIFIER_WARNED = True
    return _DEFAULT_CLASSIFIER


def classify_task(messages: List[ChatMessage]) -> str:
    """Classify an OpenAI chat request into a MNEMOS task type."""
    task_type = _configured_task_classifier().classify(messages)
    return str(task_type or "reasoning")


def _is_openai_error_detail(detail: Any) -> bool:
    if not isinstance(detail, dict):
        return False
    error = detail.get("error")
    return (
        isinstance(error, dict)
        and isinstance(error.get("type"), str)
        and isinstance(error.get("code"), str)
    )


_PROVIDER_DISPLAY = {
    "xai": "xAI",
    "openai": "OpenAI",
    "gemini": "Google",
    "groq": "Groq",
    "anthropic": "Anthropic",
    "perplexity": "Perplexity",
    "together": "Together",
    "mistral": "Mistral",
    "deepseek": "DeepSeek",
}


def _owned_by(provider: Optional[str]) -> str:
    if not provider:
        return "Unknown"
    return _PROVIDER_DISPLAY.get(provider.lower(), provider.capitalize())


def _row_model_id(row: Any) -> str:
    return row["model_id"] if hasattr(row, "__getitem__") else row.get("model_id")


def _row_provider(row: Any) -> Optional[str]:
    return row["provider"] if hasattr(row, "__getitem__") else row.get("provider")


async def list_models(user: Any) -> ModelsResponse:
    rows = await openai_compat_repo.fetch_available_models()
    models = [
        ModelInfo(id=_row_model_id(row), owned_by=_owned_by(_row_provider(row)))
        for row in rows
    ]
    return ModelsResponse(data=models)


async def get_model(model_id: str, user: Any) -> ModelInfo:
    resolved_model = MODEL_ALIASES.get(model_id, model_id)

    try:
        provider = await openai_compat_repo.fetch_model_provider(resolved_model)
    except openai_compat_repo.ModelRegistryUnavailable as exc:
        raise OpenAICompatError(status_code=503, detail="model registry unavailable") from exc

    if provider is None:
        raise OpenAICompatError(status_code=404, detail="model not found")

    return ModelInfo(id=resolved_model, owned_by=_owned_by(provider))


async def search_memory_context(*args: Any, **kwargs: Any) -> Any:
    return await openai_compat_repo.fetch_memory_context(*args, **kwargs)


async def get_model_recommendation(*args: Any, **kwargs: Any) -> Any:
    return await openai_compat_repo.fetch_model_recommendation(*args, **kwargs)


async def chat_completion(
    request: ChatCompletionRequest,
    user: Any,
    *,
    inject_memory: bool = True,
    search_context: Callable[..., Any] = openai_compat_repo.fetch_memory_context,
    get_model_recommendation: Callable[..., Any] = openai_compat_repo.fetch_model_recommendation,
    route_to_provider_response: Callable[..., Any] = _route_to_provider_response,
    route_to_provider_stream: Callable[..., Any] = _route_to_provider_stream,
) -> ChatCompletionResponse | StreamingChatCompletion:
    if not request.messages:
        raise OpenAICompatError(status_code=400, detail="messages required")
    _validate_request_messages(request.messages)

    last_msg = _last_user_message_text(request.messages)

    if not last_msg:
        raise OpenAICompatError(status_code=400, detail="No user message found")

    task_type = classify_task(request.messages)

    mnemos_docs = []
    if inject_memory:
        logger.info("[MNEMOS] task_type=%s, searching memory...", task_type)
        mnemos_docs = await search_context(last_msg, user, limit=3)
    else:
        logger.info("[MNEMOS] task_type=%s, memory injection disabled", task_type)

    model = request.model or "gpt-4o"
    if model in MODEL_ALIASES:
        model = MODEL_ALIASES[model]

    if model == "auto":
        logger.info("[MNEMOS] model=auto requested, querying optimizer for task_type=%s", task_type)
        recommendation = await get_model_recommendation(task_type=task_type)
        if recommendation:
            model = f"{recommendation['provider']}/{recommendation['model_id']}"
            logger.info(
                "[MNEMOS] Optimizer recommended %s (cost=$%.2f/MTok)",
                recommendation["model_id"],
                recommendation["cost_per_mtok"],
            )
        else:
            logger.info("[MNEMOS] Optimizer failed, using default gpt-4o")
            model = "gpt-4o"

    logger.info("[MNEMOS] model=%s", model)

    system_prompt = ""
    for msg in request.messages:
        if msg.role == "system":
            system_prompt = _content_text(msg.content)
            break

    if mnemos_docs:
        context_str = "\n\n".join([f"[Memory]\n{doc['content'][:500]}" for doc in mnemos_docs])
        system_prompt += f"\n\n[MNEMOS Context - {len(mnemos_docs)} memories]\n{context_str}"
        logger.info("[MNEMOS] Injected %s memories into context", len(mnemos_docs))

    messages: list[dict[str, Any]] = []
    system_added = False

    for msg in request.messages:
        if msg.role == "system":
            if not system_added:
                system_message = {"role": "system", "content": system_prompt}
                if msg.name is not None:
                    system_message["name"] = msg.name
                messages.append(system_message)
                system_added = True
        else:
            messages.append(_message_to_dict(msg))

    if not system_added and system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})

    generation_params = _generation_params(request)
    request_params = _request_params(request)
    now = int(datetime.now(timezone.utc).timestamp())

    if request.stream:
        stream_id = f"chatcmpl-mnemos-{now}"
        provider_stream = route_to_provider_stream(
            model=model,
            messages=messages,
            generation_params=generation_params,
            request_params=request_params,
            user=user,
        )
        try:
            first_delta = await anext(provider_stream)
        except StopAsyncIteration:
            first_delta = None
        except OpenAICompatError:
            raise
        except Exception as e:
            logger.error("[MNEMOS] Streaming request failed before response start: %s", e)
            raise _stream_preflight_exception(e) from e

        return StreamingChatCompletion(
            events=stream_event_source(
                provider_stream=provider_stream,
                first_delta=first_delta,
                stream_id=stream_id,
                created=now,
                model=model,
            )
        )

    try:
        provider_response = await route_to_provider_response(
            model=model,
            messages=messages,
            generation_params=generation_params,
            request_params=request_params,
            user=user,
        )
    except OpenAICompatError:
        raise
    except Exception as e:
        logger.error("[MNEMOS] Request failed: %s", e)
        raise OpenAICompatError(status_code=503, detail=f"Request failed: {str(e)}") from e

    choices = _provider_choices(provider_response)
    prompt_tokens = sum(len(_content_text(m.get("content")).split()) for m in messages)
    completion_tokens = len(_completion_text_for_usage(choices).split())

    return ChatCompletionResponse(
        id=f"chatcmpl-mnemos-{now}",
        created=now,
        model=model,
        choices=choices,
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )
