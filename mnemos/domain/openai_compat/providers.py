import logging
from typing import Any, Callable, Dict, List, Optional

from mnemos.core.provider_registry import GRAEAE_REGISTRY_MAP
from mnemos.db import openai_compat_repo
from mnemos.domain.graeae.engine import get_graeae_engine

from .content import _flatten_messages_for_prompt, _has_content_blocks, _has_message_names, _plain_value
from .schemas import ChatCompletionRequest

logger = logging.getLogger(__name__)

TASK_CAPABILITY_MAP = {
    "code_generation": ["coding"],
    "reasoning": ["reasoning", "logic"],
    "architecture_design": ["reasoning"],
    "summarization": ["reasoning"],
    "web_search": ["online", "search"],
}


MODEL_ALIASES = {
    "best-coding": "gpt-4o",  # Fast, strong at code generation
    "best-reasoning": "claude-3-5-sonnet-20241022",
    "fastest": "llama-3.3-70b-versatile",  # Groq
    "cheapest": "llama-2-70b",  # Ollama fallback
}


_REGISTRY_PROVIDER_TO_GRAEAE: Dict[str, str] = {
    cfg["registry_provider"]: name
    for name, cfg in GRAEAE_REGISTRY_MAP.items()
}


class OpenAICompatError(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _generation_params(request: ChatCompletionRequest) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for field in ("temperature", "max_tokens", "top_p"):
        value = getattr(request, field)
        if value is not None:
            params[field] = value
    return params


def _request_params(request: ChatCompletionRequest) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    for field in (
        "tools", "tool_choice", "response_format", "stop", "n",
        "presence_penalty", "frequency_penalty",
    ):
        value = getattr(request, field)
        if value is not None:
            params[field] = _plain_value(value)
    if request.user is not None:
        params["user"] = request.user
    return params


def _provider_supports_tools(provider: str, cfg: Dict[str, Any]) -> bool:
    if "supports_tools" in cfg:
        return bool(cfg["supports_tools"])
    return provider == "openai" or cfg.get("api") == "anthropic"


def _provider_supports_response_format(provider: str, cfg: Dict[str, Any]) -> bool:
    if "supports_response_format" in cfg:
        return bool(cfg["supports_response_format"])
    return cfg.get("api") in {"openai", "gemini"}


def _provider_supports_multimodal(provider: str, cfg: Dict[str, Any], model: str) -> bool:
    if "supports_vision" in cfg:
        return bool(cfg["supports_vision"])
    if cfg.get("api") in {"anthropic", "gemini"}:
        return True
    lower_model = (model or cfg.get("model") or "").lower()
    if provider == "openai" and any(token in lower_model for token in ("gpt-4o", "gpt-5", "vision")):
        return True
    return False


def _provider_supports_stop(cfg: Dict[str, Any]) -> bool:
    return cfg.get("api") in {"openai", "anthropic", "gemini"}


def _provider_supports_n(cfg: Dict[str, Any]) -> bool:
    return cfg.get("api") in {"openai", "gemini"}


def _provider_supports_penalties(cfg: Dict[str, Any]) -> bool:
    return cfg.get("api") in {"openai", "gemini"}


def _validate_anthropic_tool_choice(provider: str, tool_choice: Any) -> None:
    if tool_choice is None:
        return
    if isinstance(tool_choice, str):
        if tool_choice in {"auto", "any", "none", "required"}:
            return
        raise OpenAICompatError(
            status_code=400,
            detail=f"provider {provider} does not support tool_choice {tool_choice!r}",
        )
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        if tool_choice.get("type") == "function" and isinstance(fn, dict) and fn.get("name"):
            return
    raise OpenAICompatError(
        status_code=400,
        detail=(
            f"provider {provider} only supports tool_choice strings "
            "auto, none, required, any, or a function tool selector"
        ),
    )


def _validate_provider_roles(provider: str, provider_cfg: Dict[str, Any], messages: List[Dict[str, Any]]) -> None:
    supported = _ROLE_SUPPORT_BY_API.get(provider_cfg.get("api"))
    if supported is None:
        return
    supported_set = set(supported)
    for msg in messages:
        role = msg.get("role", "user")
        if role not in supported_set:
            raise OpenAICompatError(
                status_code=400,
                detail=f"provider {provider} does not support role={role}; supported: {', '.join(supported)}",
            )


def _validate_provider_request(
    provider: str,
    provider_cfg: Dict[str, Any],
    model: str,
    messages: List[Dict[str, Any]],
    request_params: Dict[str, Any],
) -> None:
    _validate_provider_roles(provider, provider_cfg, messages)
    if _has_message_names(messages) and provider_cfg.get("api") != "openai":
        raise OpenAICompatError(status_code=400, detail=f"provider {provider} does not support message name")
    if _has_content_blocks(messages) and not _provider_supports_multimodal(provider, provider_cfg, model):
        raise OpenAICompatError(
            status_code=400,
            detail=f"provider {provider} does not support multimodal content blocks",
        )
    if ("tools" in request_params or "tool_choice" in request_params) and not _provider_supports_tools(
        provider, provider_cfg,
    ):
        raise OpenAICompatError(status_code=400, detail=f"provider {provider} does not support tool_calls")
    if "tool_choice" in request_params and provider_cfg.get("api") == "anthropic":
        _validate_anthropic_tool_choice(provider, request_params["tool_choice"])
    if "response_format" in request_params and not _provider_supports_response_format(provider, provider_cfg):
        raise OpenAICompatError(status_code=400, detail=f"provider {provider} does not support response_format")
    if (
        "response_format" in request_params
        and provider_cfg.get("api") == "gemini"
        and request_params["response_format"].get("type") != "json_object"
    ):
        raise OpenAICompatError(
            status_code=400,
            detail=f"provider {provider} only supports response_format type json_object",
        )
    if "stop" in request_params and not _provider_supports_stop(provider_cfg):
        raise OpenAICompatError(status_code=400, detail=f"provider {provider} does not support stop")
    if "n" in request_params and not _provider_supports_n(provider_cfg):
        raise OpenAICompatError(status_code=400, detail=f"provider {provider} does not support n")
    if (
        ("presence_penalty" in request_params or "frequency_penalty" in request_params)
        and not _provider_supports_penalties(provider_cfg)
    ):
        raise OpenAICompatError(status_code=400, detail=f"provider {provider} does not support penalties")


def _fallback_provider_from_name(model: str) -> Optional[str]:
    """Last-resort resolver used when model_registry has no row for
    the requested model_id. Returns a provider name or None (unknown)."""
    lower = model.lower()
    for key, mapped in _FALLBACK_PROVIDER_MAP.items():
        if key in lower:
            return mapped
    return None


def _model_not_found_error(model: str) -> dict[str, dict[str, str]]:
    return {
        "error": {
            "message": f"The model `{model}` does not exist or you do not have access to it.",
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }


def _strip_gateway_namespace(model: str, provider: str) -> str:
    candidate_prefixes = [f"{provider}/"]
    if provider in GRAEAE_REGISTRY_MAP:
        registry_name = GRAEAE_REGISTRY_MAP[provider]["registry_provider"]
        if registry_name != provider:
            candidate_prefixes.append(f"{registry_name}/")

    for pfx in candidate_prefixes:
        if model.startswith(pfx):
            return model[len(pfx):]
    return model


_ROLE_SUPPORT_BY_API = {
    "gemini": ("system", "user", "assistant"),
    "anthropic": ("system", "user", "assistant", "tool"),
    # "function" is OpenAI's deprecated function-message role. We allow it
    # through unchanged for OpenAI-compatible providers that still support it.
    "openai": ("system", "user", "assistant", "tool", "function"),
}


_FALLBACK_PROVIDER_MAP = {
    "claude":   "claude",
    "gpt-":     "openai",
    "llama":    "groq",
    "deepseek": "groq",
    "sonar":    "perplexity",
    "grok":     "xai",
    "gemini":   "gemini",
    "mistral":  "together",
}


async def _resolve_provider_for_model(model: str) -> Optional[str]:
    provider = await openai_compat_repo.lookup_provider_for_model(model)
    if provider is not None:
        return _REGISTRY_PROVIDER_TO_GRAEAE.get(provider, provider)
    return _fallback_provider_from_name(model)


async def _prepare_provider_route(
    model: str,
    messages: List[Dict[str, Any]],
    request_params: Optional[Dict[str, Any]] = None,
    *,
    resolve_provider: Optional[Callable[[str], Any]] = None,
    get_engine: Optional[Callable[[], Any]] = None,
) -> tuple[Any, str, str, str]:
    if not messages:
        raise OpenAICompatError(status_code=400, detail="messages required")

    prompt = _flatten_messages_for_prompt(messages)
    resolver = resolve_provider or _resolve_provider_for_model
    provider = await resolver(model)
    if provider is None:
        logger.warning(
            "[MNEMOS] unknown model %r - not in model_registry and no "
            "fallback substring match", model,
        )
        raise OpenAICompatError(
            status_code=404,
            detail=_model_not_found_error(model),
        )

    bare_model = _strip_gateway_namespace(model, provider)
    engine_factory = get_engine or get_graeae_engine
    graeae = engine_factory()
    provider_cfg = dict(graeae.providers.get(provider, {}))
    if bare_model:
        provider_cfg["model"] = bare_model
    _validate_provider_request(provider, provider_cfg, bare_model, messages, request_params or {})

    logger.info(
        "[MNEMOS] Route: model=%s -> provider=%s (messages=%s, prompt_chars=%s)",
        model, provider, len(messages), len(prompt),
    )
    return graeae, provider, bare_model, prompt


async def _route_to_provider_response(
    model: str,
    messages: List[Dict[str, Any]],
    generation_params: Optional[Dict[str, Any]] = None,
    request_params: Optional[Dict[str, Any]] = None,
    user: Optional[Any] = None,
    *,
    resolve_provider: Optional[Callable[[str], Any]] = None,
    get_engine: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    graeae, provider, bare_model, prompt = await _prepare_provider_route(
        model=model,
        messages=messages,
        request_params=request_params,
        resolve_provider=resolve_provider,
        get_engine=get_engine,
    )

    try:
        response = await graeae.route(
            provider,
            bare_model,
            prompt,
            task_type="reasoning",
            timeout=30,
            generation_params=generation_params,
            request_params=request_params,
            messages=messages,
        )

        if response.get("status") == "success":
            return response
        cause = response.get("error") or response.get("status") or "unknown"
        logger.error(
            "[MNEMOS] Provider %s unavailable: %s (status=%s)",
            provider, cause, response.get("status"),
        )
        raise OpenAICompatError(
            status_code=503,
            detail=f"Provider {provider} unavailable: {cause}",
        )

    except OpenAICompatError:
        raise
    except Exception as e:
        logger.error("[MNEMOS] Routing to %s failed: %s", provider, e)
        raise OpenAICompatError(status_code=503, detail=f"Routing error: {str(e)}")


async def _route_to_provider(
    model: str,
    messages: List[Dict[str, Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
    user: Any,
    top_p: Optional[float] = None,
    request_params: Optional[Dict[str, Any]] = None,
    *,
    resolve_provider: Optional[Callable[[str], Any]] = None,
    get_engine: Optional[Callable[[], Any]] = None,
) -> str:
    generation_params: Dict[str, Any] = {}
    if temperature is not None:
        generation_params["temperature"] = temperature
    if max_tokens is not None:
        generation_params["max_tokens"] = max_tokens
    if top_p is not None:
        generation_params["top_p"] = top_p
    response = await _route_to_provider_response(
        model=model,
        messages=messages,
        generation_params=generation_params,
        request_params=request_params,
        user=user,
        resolve_provider=resolve_provider,
        get_engine=get_engine,
    )
    return response.get("response_text", "")
