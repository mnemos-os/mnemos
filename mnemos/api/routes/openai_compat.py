"""
OpenAI-Compatible Gateway for MNEMOS

Provides `/v1/chat/completions` and `/v1/models` endpoints compatible with OpenAI SDK.
All claw systems authenticate with a single MNEMOS bearer token; MNEMOS manages provider keys.

Model selection:
  - explicit model name: passthrough to that provider (user pulls from /v1/models)
  - model="auto": optimizer recommends model based on task type and cost budget
  - model="best-coding", etc.: resolve alias to concrete model

Memory injection:
  - Semantic search on last user message
  - ARTEMIS-compress relevant context (512-token budget)
  - Add to system prompt with [MNEMOS context] header
"""

import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

import mnemos.core.lifecycle as _lc
from mnemos.api.dependencies import UserContext, get_current_user
from mnemos.domain.graeae.engine import _REGISTRY_MAP, ProviderStreamError, get_graeae_engine

# Reverse the engine's _REGISTRY_MAP so we can translate the
# model_registry's `provider` column (e.g. "anthropic") back into the
# GRAEAE-engine provider key (e.g. "claude") that engine.route() looks
# up via self.providers. Only `anthropic → claude` actually flips today,
# but build the full reverse so future mismatches are absorbed
# automatically without touching this resolver.
_REGISTRY_PROVIDER_TO_GRAEAE: Dict[str, str] = {
    cfg["registry_provider"]: name
    for name, cfg in _REGISTRY_MAP.items()
}

logger = logging.getLogger(__name__)
router = APIRouter(tags=["openai"])

# Model capability mapping for task-type routing
TASK_CAPABILITY_MAP = {
    "code_generation": ["coding"],
    "reasoning": ["reasoning", "logic"],
    "architecture_design": ["reasoning"],
    "summarization": ["reasoning"],
    "web_search": ["online", "search"],
}

# Model aliases for convenience
MODEL_ALIASES = {
    "best-coding": "gpt-4o",  # Fast, strong at code generation
    "best-reasoning": "claude-3-5-sonnet-20241022",
    "fastest": "llama-3.3-70b-versatile",  # Groq
    "cheapest": "llama-2-70b",  # Ollama fallback
}


class ContentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text", "image_url"]
    text: Optional[str] = None
    image_url: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_payload(self):
        if self.type == "text" and self.text is None:
            raise ValueError("text content block requires text")
        if self.type == "image_url" and self.image_url is None:
            raise ValueError("image_url content block requires image_url")
        return self


ChatContent = Union[str, List[ContentBlock]]


class ToolFunction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    strict: Optional[bool] = None


class Tool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["function"]
    function: ToolFunction


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: Optional[ChatContent] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    function_call: Optional[Dict[str, Any]] = Field(default=None, exclude=True)


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Optional[str] = "auto"
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    stream: bool = False
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    response_format: Optional[Dict[str, Any]] = None
    stop: Optional[Union[str, List[str]]] = None
    n: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None


class ChatCompletionStreamRequest(ChatCompletionRequest):
    stream: bool = True


class ChatCompletionResponseMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: Optional[ChatContent] = None
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    function_call: Optional[Dict[str, Any]] = None
    refusal: Optional[str] = None
    audio: Optional[Dict[str, Any]] = None
    annotations: Optional[List[Dict[str, Any]]] = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatCompletionResponseMessage
    finish_reason: str


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Dict[str, int]


class ChatCompletionDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None


class ChatCompletionStreamChoice(BaseModel):
    index: int
    delta: ChatCompletionDelta
    finish_reason: Optional[str] = None


class ChatCompletionStreamResponse(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionStreamChoice]


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    owned_by: str


class ModelsResponse(BaseModel):
    object: str = "list"
    data: List[ModelInfo]


async def _search_mnemos_context(query: str, user: UserContext, limit: int = 5) -> List[Dict[str, Any]]:
    """Search MNEMOS for relevant context based on user query.

    Returns list of dicts with 'id' and 'content' keys for memory
    injection into /v1/chat/completions. Non-root callers are scoped
    to their owner_id AND namespace (v3.1.2 Tier 3 two-dimensional
    gate). Previously this path filtered on owner_id alone, which
    let cross-namespace memories leak into the gateway's injected
    context under the same owner.
    """
    if not _lc._pool:
        logger.debug("[MNEMOS] No DB pool available")
        return []

    is_root = user.role == "root"

    try:
        async with _lc._pool.acquire() as conn:
            # Full-text search on content + category filtering. Explicit
            # to_tsvector so we match the 'english' dictionary regardless of
            # the cluster's default_text_search_config and so the index (if
            # present) can actually be used.
            # Compression-in-hot-paths: prefer the contest winner's
            # compressed_content when available, then fall back to raw content. Saves
            # prompt-window tokens on memories that have been
            # through the compression pipeline — the whole point of
            # running the contest. Non-winners' compressed forms
            # (candidates) intentionally NOT used; we only surface
            # the audit-approved winner.
            if is_root:
                memories = await conn.fetch(
                    """
                    SELECT m.id, m.category,
                           COALESCE(v.compressed_content, m.content) AS content
                    FROM memories m
                    LEFT JOIN memory_compressed_variants v
                        ON v.memory_id = m.id
                    WHERE
                        to_tsvector('english', m.content) @@ plainto_tsquery('english', $1)
                        OR m.category IN ('solutions', 'patterns', 'decisions', 'infrastructure')
                    ORDER BY m.updated DESC NULLS LAST
                    LIMIT $2
                    """,
                    query,
                    limit,
                )
            else:
                # Slice 2.1: full v1_multiuser-mirror visibility
                # predicate (owner / federation / world / group) via
                # the shared module, aliased to the JOIN's m. table
                # reference. Same predicate as list/get/search/
                # rehydrate so a memory visible elsewhere also lands
                # in gateway context injection. Mutation paths keep
                # strict owner_id scoping; only reads honor the
                # broader contract.
                from mnemos.core.visibility import read_visibility_predicate
                vis_clause, vis_params = read_visibility_predicate(
                    user.user_id, list(user.group_ids),
                    start_param_idx=1, table_alias="m",
                )
                # Visibility params occupy $1..$N; namespace, query,
                # limit follow at $N+1, $N+2, $N+3.
                ns_ph = f"${len(vis_params) + 1}"
                q_ph = f"${len(vis_params) + 2}"
                lim_ph = f"${len(vis_params) + 3}"
                memories = await conn.fetch(
                    f"""
                    SELECT m.id, m.category,
                           COALESCE(v.compressed_content, m.content) AS content
                    FROM memories m
                    LEFT JOIN memory_compressed_variants v
                        ON v.memory_id = m.id
                    WHERE {vis_clause}
                      AND m.namespace = {ns_ph}
                      AND (
                          to_tsvector('english', m.content) @@ plainto_tsquery('english', {q_ph})
                          OR m.category IN ('solutions', 'patterns', 'decisions', 'infrastructure')
                      )
                    ORDER BY m.updated DESC NULLS LAST
                    LIMIT {lim_ph}
                    """,
                    *vis_params,
                    user.namespace,
                    query,
                    limit,
                )
            logger.info(f"[MNEMOS] Found {len(memories)} memories for query '{query[:30]}...'")
            return [{"id": m["id"], "content": m["content"]} for m in memories]
    except Exception as e:
        logger.warning(f"[MNEMOS] Search failed for '{query[:50]}...': {e}")
        return []


async def _get_model_recommendation(
    task_type: str,
    cost_budget: float = 10.0,
    quality_floor: float = 0.85,
) -> Optional[Dict[str, Any]]:
    """Query model optimizer for cost-aware model recommendation.

    Calls the /model-registry/recommend endpoint to find cheapest model
    meeting quality + capability requirements for the task_type.
    """
    pool = _lc._pool
    if not pool:
        logger.warning("[OPTIMIZER] No DB pool available")
        return None

    try:
        async with pool.acquire() as conn:
            # Map task types to required capabilities
            capability_map = {
                "code_generation": ["coding"],
                "reasoning": ["reasoning", "logic"],
                "architecture_design": ["reasoning"],
                "summarization": ["reasoning"],
                "web_search": ["online", "search"],
            }
            required_caps = capability_map.get(task_type, ["reasoning"])

            # Find models meeting criteria
            models = await conn.fetch(
                """
                SELECT
                    provider, model_id, display_name, input_cost_per_mtok,
                    output_cost_per_mtok, capabilities, graeae_weight, context_window
                FROM model_registry
                WHERE available = true
                AND deprecated = false
                AND graeae_weight >= $1
                AND (input_cost_per_mtok + output_cost_per_mtok) / 2.0 <= $2
                AND capabilities @> $3
                ORDER BY (input_cost_per_mtok + output_cost_per_mtok) ASC
                LIMIT 1
                """,
                quality_floor,
                cost_budget,
                required_caps,
            )

            if not models:
                # Fallback: cheapest model available (ignore budget)
                logger.info(
                    f"[OPTIMIZER] No model found for {task_type} "
                    f"(budget=${cost_budget}/MTok, quality>={quality_floor}), "
                    f"using fallback cheapest model"
                )
                models = await conn.fetch(
                    """
                    SELECT
                        provider, model_id, display_name, input_cost_per_mtok,
                        output_cost_per_mtok, capabilities, graeae_weight, context_window
                    FROM model_registry
                    WHERE available = true AND deprecated = false
                    ORDER BY (input_cost_per_mtok + output_cost_per_mtok) ASC
                    LIMIT 1
                    """
                )

            if not models:
                logger.warning("[OPTIMIZER] No models available, using default gpt-4o")
                return None

            model = models[0]
            avg_cost = (model["input_cost_per_mtok"] + model["output_cost_per_mtok"]) / 2.0

            logger.info(
                f"[OPTIMIZER] Recommended {model['provider']}/{model['model_id']} "
                f"for {task_type} (cost=${avg_cost:.2f}/MTok)"
            )

            return {
                "provider": model["provider"],
                "model_id": model["model_id"],
                "display_name": model.get("display_name"),
                "cost_per_mtok": avg_cost,
                "quality_score": model["graeae_weight"],
                "context_window": model.get("context_window"),
            }

    except Exception as e:
        logger.warning(f"[OPTIMIZER] Recommendation failed: {e}, using default")
        return None


def _serialize_content(content: Any) -> Any:
    """Convert Pydantic content blocks into plain dicts for provider payloads."""
    if isinstance(content, list):
        serialized = []
        for block in content:
            if isinstance(block, BaseModel):
                serialized.append(block.model_dump(exclude_none=True))
            else:
                serialized.append(block)
        return serialized
    return content


def _plain_value(value: Any) -> Any:
    """Recursively convert request model fragments into provider payload data."""
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    if isinstance(value, list):
        return [_plain_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain_value(item) for key, item in value.items() if item is not None}
    return value


def _content_text(content: Any) -> str:
    """Extract searchable/flattenable text from OpenAI string or content-block payloads."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            block_data = block.model_dump(exclude_none=True) if isinstance(block, BaseModel) else block
            if not isinstance(block_data, dict):
                continue
            if block_data.get("type") == "text":
                parts.append(str(block_data.get("text", "")))
            elif block_data.get("type") == "image_url":
                image_url = block_data.get("image_url") or {}
                url = image_url.get("url") if isinstance(image_url, dict) else None
                if url:
                    parts.append(f"[image_url: {url}]")
        return "\n".join(p for p in parts if p)
    return str(content)


def _message_to_dict(msg: ChatMessage) -> Dict[str, Any]:
    data: Dict[str, Any] = {"role": msg.role}
    if msg.content is not None:
        data["content"] = _serialize_content(msg.content)
    if msg.name is not None:
        data["name"] = msg.name
    if msg.tool_calls is not None:
        data["tool_calls"] = msg.tool_calls
    if msg.tool_call_id is not None:
        data["tool_call_id"] = msg.tool_call_id
    return data


def _has_content_blocks(messages: List[Dict[str, Any]]) -> bool:
    return any(isinstance(msg.get("content"), list) for msg in messages)


def _has_message_names(messages: List[Dict[str, Any]]) -> bool:
    return any(msg.get("name") is not None for msg in messages)


def _flatten_messages_for_prompt(messages: List[Dict[str, Any]]) -> str:
    """Serialize a chat-completions ``messages`` array to a single prompt string.

    Used as a fallback when GRAEAE's single-provider route accepts only a
    flat prompt. Preserves role boundaries so a provider that was given a
    system prompt, prior assistant turns, and a fresh user question sees
    all three, not just the last user message (regression for #M31-02).
    """
    parts: List[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = _content_text(msg.get("content"))
        if not content:
            continue
        if role == "system":
            parts.append(f"[System]\n{content}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}")
        elif role == "tool":
            parts.append(f"[Tool]\n{content}")
        else:
            parts.append(f"[User]\n{content}")
    return "\n\n".join(parts)


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
        raise HTTPException(
            status_code=400,
            detail=f"provider {provider} does not support tool_choice {tool_choice!r}",
        )
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        if tool_choice.get("type") == "function" and isinstance(fn, dict) and fn.get("name"):
            return
    raise HTTPException(
        status_code=400,
        detail=(
            f"provider {provider} only supports tool_choice strings "
            "auto, none, required, any, or a function tool selector"
        ),
    )


_ROLE_SUPPORT_BY_API = {
    "gemini": ("system", "user", "assistant"),
    "anthropic": ("system", "user", "assistant", "tool"),
    # "function" is OpenAI's deprecated function-message role. We allow it
    # through unchanged for OpenAI-compatible providers that still support it.
    "openai": ("system", "user", "assistant", "tool", "function"),
}


def _validate_provider_roles(provider: str, provider_cfg: Dict[str, Any], messages: List[Dict[str, Any]]) -> None:
    supported = _ROLE_SUPPORT_BY_API.get(provider_cfg.get("api"))
    if supported is None:
        return
    supported_set = set(supported)
    for msg in messages:
        role = msg.get("role", "user")
        if role not in supported_set:
            raise HTTPException(
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
        raise HTTPException(status_code=400, detail=f"provider {provider} does not support message name")
    if _has_content_blocks(messages) and not _provider_supports_multimodal(provider, provider_cfg, model):
        raise HTTPException(
            status_code=400,
            detail=f"provider {provider} does not support multimodal content blocks",
        )
    if ("tools" in request_params or "tool_choice" in request_params) and not _provider_supports_tools(
        provider, provider_cfg,
    ):
        raise HTTPException(status_code=400, detail=f"provider {provider} does not support tool_calls")
    if "tool_choice" in request_params and provider_cfg.get("api") == "anthropic":
        _validate_anthropic_tool_choice(provider, request_params["tool_choice"])
    if "response_format" in request_params and not _provider_supports_response_format(provider, provider_cfg):
        raise HTTPException(status_code=400, detail=f"provider {provider} does not support response_format")
    if (
        "response_format" in request_params
        and provider_cfg.get("api") == "gemini"
        and request_params["response_format"].get("type") != "json_object"
    ):
        raise HTTPException(
            status_code=400,
            detail=f"provider {provider} only supports response_format type json_object",
        )
    if "stop" in request_params and not _provider_supports_stop(provider_cfg):
        raise HTTPException(status_code=400, detail=f"provider {provider} does not support stop")
    if "n" in request_params and not _provider_supports_n(provider_cfg):
        raise HTTPException(status_code=400, detail=f"provider {provider} does not support n")
    if (
        ("presence_penalty" in request_params or "frequency_penalty" in request_params)
        and not _provider_supports_penalties(provider_cfg)
    ):
        raise HTTPException(status_code=400, detail=f"provider {provider} does not support penalties")


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
            raise HTTPException(
                status_code=502,
                detail=f"provider returned unsupported response field {key}; gateway cannot faithfully represent",
            )
    return dict(message)


def _provider_choices(response: Dict[str, Any]) -> List[ChatCompletionChoice]:
    raw_choices = response.get("choices") or []
    choices: List[ChatCompletionChoice] = []
    for i, choice in enumerate(raw_choices):
        message_data = _response_message_data(choice.get("message") or {
            "role": "assistant",
            "content": choice.get("text") or "",
        })
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
            message=ChatCompletionResponseMessage(role="assistant", content=response.get("response_text", "")),
            finish_reason=response.get("finish_reason") or "stop",
        )
    ]


def _completion_text_for_usage(choices: List[ChatCompletionChoice]) -> str:
    return "\n".join(_content_text(choice.message.content) for choice in choices)


def _validate_request_messages(messages: List[ChatMessage]) -> None:
    for msg in messages:
        if msg.function_call is not None:
            raise HTTPException(
                status_code=400,
                detail="message.function_call is deprecated; use tool_calls and tool messages instead",
            )


def _stream_event(data: Dict[str, Any]) -> str:
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n"


def _stream_error_event(message: str, error_type: str = "provider_stream_error") -> str:
    return _stream_event({"error": {"message": message, "type": error_type}})


def _stream_preflight_exception(exc: Exception) -> HTTPException:
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
    return HTTPException(status_code=status_code, detail=f"Streaming request failed: {message}")


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


# Substring-to-provider heuristics kept as a last-resort fallback when
# model_registry is empty (fresh install without a seeded registry).
# Ordering matters — first match wins, so more-specific tokens go
# first (`gpt-` matches `gpt-5` before `gpt-4`). Updated for 2026
# frontier names; entries stay broad so drift between models like
# "gpt-5.2-chat-latest" and "gpt-5-mini" both land on `openai`.
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


def _is_openai_error_detail(detail: Any) -> bool:
    if not isinstance(detail, dict):
        return False
    error = detail.get("error")
    return (
        isinstance(error, dict)
        and isinstance(error.get("type"), str)
        and isinstance(error.get("code"), str)
    )


def _openai_error_response(exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.detail)


async def _resolve_provider_for_model(model: str) -> Optional[str]:
    """Look up `model` in model_registry and return its provider.

    Two lookup attempts so callers can use either the bare upstream
    model_id (which may itself contain slashes like `meta/llama-3.3-70b-
    instruct` for NVIDIA or `Qwen/Qwen3-235B-A22B-Instruct-2507-tput`
    for Together) OR the gateway-namespaced form
    `<graeae_provider>/<bare_api_id>`:

      1. Direct: WHERE model_id = $1 — matches a bare slash-bearing
         id verbatim against the registry.
      2. Namespaced: split on the first `/`; require the head to match
         a registered provider AND the tail to match its model_id. Only
         strips the prefix when the head is actually a GRAEAE provider,
         so a bare `meta/llama-3.3-70b-instruct` doesn't get truncated
         by accident (since `meta` isn't a provider in the registry).

    Falls back to the substring-match heuristic when both lookups
    miss — preserves behavior for fresh installs without a seeded
    registry. Returns None if everything fails; the caller surfaces
    that as a 400.
    """
    if _lc._pool is not None:
        try:
            async with _lc._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT provider FROM model_registry "
                    "WHERE model_id = $1 "
                    "  AND available = true AND deprecated = false",
                    model,
                )
                if row is not None:
                    # Translate registry provider name → GRAEAE provider name.
                    # The registry stores Anthropic models under "anthropic"
                    # but GRAEAE's engine knows that provider as "claude";
                    # without this normalization, engine.route("anthropic", ...)
                    # 503s with "provider 'anthropic' not registered".
                    return _REGISTRY_PROVIDER_TO_GRAEAE.get(
                        row["provider"], row["provider"],
                    )
                if "/" in model:
                    head, tail = model.split("/", 1)
                    # Accept either a registry provider name OR a GRAEAE
                    # provider name as the head — both forms appear in
                    # the wild. Map the head to its registry name for
                    # the WHERE clause.
                    head_registry = _REGISTRY_MAP.get(
                        head, {"registry_provider": head}
                    )["registry_provider"]
                    row = await conn.fetchrow(
                        "SELECT provider FROM model_registry "
                        "WHERE provider = $1 AND model_id = $2 "
                        "  AND available = true AND deprecated = false",
                        head_registry, tail,
                    )
                    if row is not None:
                        return _REGISTRY_PROVIDER_TO_GRAEAE.get(
                            row["provider"], row["provider"],
                        )
        except Exception as exc:
            logger.warning(
                "[MNEMOS] model_registry lookup failed for model=%s: %s",
                model, exc,
            )
    return _fallback_provider_from_name(model)


def _strip_gateway_namespace(model: str, provider: str) -> str:
    candidate_prefixes = [f"{provider}/"]
    if provider in _REGISTRY_MAP:
        registry_name = _REGISTRY_MAP[provider]["registry_provider"]
        if registry_name != provider:
            candidate_prefixes.append(f"{registry_name}/")

    for pfx in candidate_prefixes:
        if model.startswith(pfx):
            return model[len(pfx):]
    return model


async def _prepare_provider_route(
    model: str,
    messages: List[Dict[str, Any]],
    request_params: Optional[Dict[str, Any]] = None,
) -> tuple[Any, str, str, str]:
    """Resolve provider, preserve model slash semantics, and validate controls."""
    if not messages:
        raise HTTPException(status_code=400, detail="messages required")

    prompt = _flatten_messages_for_prompt(messages)
    provider = await _resolve_provider_for_model(model)
    if provider is None:
        logger.warning(
            "[MNEMOS] unknown model %r — not in model_registry and no "
            "fallback substring match", model,
        )
        raise HTTPException(
            status_code=404,
            detail=_model_not_found_error(model),
        )

    bare_model = _strip_gateway_namespace(model, provider)
    graeae = get_graeae_engine()
    provider_cfg = dict(graeae.providers.get(provider, {}))
    if bare_model:
        provider_cfg["model"] = bare_model
    _validate_provider_request(provider, provider_cfg, bare_model, messages, request_params or {})

    logger.info(
        f"[MNEMOS] Route: model={model} → provider={provider} "
        f"(messages={len(messages)}, prompt_chars={len(prompt)})"
    )
    return graeae, provider, bare_model, prompt


async def _route_to_provider_response(
    model: str,
    messages: List[Dict[str, Any]],
    generation_params: Optional[Dict[str, Any]] = None,
    request_params: Optional[Dict[str, Any]] = None,
    user: Optional[UserContext] = None,
) -> Dict[str, Any]:
    """Route request to selected provider via GRAEAE single-provider mode.

    Provider resolution: query model_registry for the exact model_id,
    falling back to substring heuristics only when the registry has no row.
    Unknown models are rejected with 400 instead of silently routing to a
    default.
    """
    graeae, provider, bare_model, prompt = await _prepare_provider_route(
        model=model,
        messages=messages,
        request_params=request_params,
    )

    try:
        # Use GRAEAE single-provider route (no consensus, just direct call)
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
        # GRAEAE returns unavailable shape with an `error` field (v3.1.2).
        # Surface the cause in both the log line and the 503 detail so
        # operators see WHY the provider failed (missing key, 401, etc.)
        # without tailing debug logs.
        cause = response.get("error") or response.get("status") or "unknown"
        logger.error(
            "[MNEMOS] Provider %s unavailable: %s (status=%s)",
            provider, cause, response.get("status"),
        )
        raise HTTPException(
            status_code=503,
            detail=f"Provider {provider} unavailable: {cause}",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MNEMOS] Routing to {provider} failed: {e}")
        raise HTTPException(status_code=503, detail=f"Routing error: {str(e)}")


async def _route_to_provider_stream(
    model: str,
    messages: List[Dict[str, Any]],
    generation_params: Optional[Dict[str, Any]] = None,
    request_params: Optional[Dict[str, Any]] = None,
    user: Optional[UserContext] = None,
) -> AsyncIterator[Dict[str, Any]]:
    graeae, provider, bare_model, prompt = await _prepare_provider_route(
        model=model,
        messages=messages,
        request_params=request_params,
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
        logger.error(f"[MNEMOS] Streaming route to {provider} failed: {e}")
        raise


async def _route_to_provider(
    model: str,
    messages: List[Dict[str, Any]],
    temperature: Optional[float],
    max_tokens: Optional[int],
    user: UserContext,
    top_p: Optional[float] = None,
    request_params: Optional[Dict[str, Any]] = None,
) -> str:
    """Backward-compatible string-returning wrapper used by session routes."""
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
    )
    return response.get("response_text", "")


# Provider key -> display name for the `owned_by` field in OpenAI
# /v1/models responses. Keys match the `provider` column values in
# db/migrations_model_registry.sql (xai, openai, gemini, groq, …).
# Unknown provider keys fall back to the key capitalized.
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
    """Turn a provider key into an OpenAI-style owned_by display string."""
    if not provider:
        return "Unknown"
    return _PROVIDER_DISPLAY.get(provider.lower(), provider.capitalize())


def _row_model_id(r) -> str:
    """Support both dict fallback rows and asyncpg Record objects."""
    return r["model_id"] if hasattr(r, "__getitem__") else r.get("model_id")


def _row_provider(r) -> Optional[str]:
    return r["provider"] if hasattr(r, "__getitem__") else r.get("provider")


@router.get("/v1/models", response_model=ModelsResponse)
async def list_models(
    authorization: Optional[str] = Header(None),
    user: UserContext = Depends(get_current_user),
):
    """List available models from the model_registry table.

    Returns every row where available=true AND deprecated=false,
    ordered by graeae_weight DESC so higher-quality models lead the
    response. Discovery is intentionally registry-only: fallback routing
    can still serve explicit chat requests, but `/v1/models` does not
    advertise synthetic models that are not registered.
    """
    rows: list = []
    if _lc._pool is not None:
        try:
            async with _lc._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT provider, model_id, display_name
                    FROM model_registry
                    WHERE available = true AND deprecated = false
                    ORDER BY graeae_weight DESC NULLS LAST, model_id ASC
                    """
                )
        except Exception as exc:
            logger.warning(
                "[/v1/models] model_registry query failed, "
                "returning an empty discovery list: %s", exc,
            )
            rows = []

    models = [
        ModelInfo(id=_row_model_id(r), owned_by=_owned_by(_row_provider(r)))
        for r in rows
    ]
    return ModelsResponse(data=models)


@router.get("/v1/models/{model_id}")
async def get_model(
    model_id: str,
    authorization: Optional[str] = Header(None),
    user: UserContext = Depends(get_current_user),
):
    """Look up a single model in the registry.

    Aliases resolve first (best-coding etc. → concrete model), then
    the resolved id is checked against model_registry. Unlike chat
    routing, model discovery is registry-only: unregistered IDs return
    404 instead of synthetic `owned_by="Unknown"` metadata.
    """
    resolved_model = MODEL_ALIASES.get(model_id, model_id)
    provider: Optional[str] = None

    if _lc._pool is not None:
        try:
            async with _lc._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT provider
                    FROM model_registry
                    WHERE model_id = $1
                      AND available = true
                      AND deprecated = false
                    LIMIT 1
                    """,
                    resolved_model,
                )
                if row is not None:
                    provider = row["provider"]
        except Exception as exc:
            logger.warning(
                "[/v1/models/%s] registry lookup failed: %s",
                model_id, exc,
            )
            raise HTTPException(status_code=503, detail="model registry unavailable") from exc

    if provider is None:
        raise HTTPException(status_code=404, detail="model not found")

    return ModelInfo(id=resolved_model, owned_by=_owned_by(provider))


@router.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    request: ChatCompletionRequest,
    authorization: Optional[str] = Header(None),
    user: UserContext = Depends(get_current_user),
):
    """OpenAI-compatible chat completions endpoint with memory injection."""

    if not request.messages:
        raise HTTPException(status_code=400, detail="messages required")
    _validate_request_messages(request.messages)

    # Extract last user message for context search and task detection
    last_msg = ""
    for msg in reversed(request.messages):
        if msg.role == "user":
            last_msg = _content_text(msg.content)
            break

    if not last_msg:
        raise HTTPException(status_code=400, detail="No user message found")

    # Determine task type from content
    task_type = "reasoning"
    if any(kw in last_msg.lower() for kw in ["code", "function", "class", "def", "import", "syntax"]):
        task_type = "code_generation"
    elif any(kw in last_msg.lower() for kw in ["arch", "design", "pattern", "structure", "system"]):
        task_type = "architecture_design"

    logger.info(f"[MNEMOS] task_type={task_type}, searching memory...")

    # Search MNEMOS for context (non-blocking, graceful fallback)
    mnemos_docs = await _search_mnemos_context(last_msg, user, limit=3)

    # Resolve and validate model
    model = request.model or "gpt-4o"
    if model in MODEL_ALIASES:
        model = MODEL_ALIASES[model]

    # Handle auto model selection via optimizer
    if model == "auto":
        logger.info(f"[MNEMOS] model=auto requested, querying optimizer for task_type={task_type}")
        recommendation = await _get_model_recommendation(task_type=task_type)
        if recommendation:
            model = f"{recommendation['provider']}/{recommendation['model_id']}"
            logger.info(
                f"[MNEMOS] Optimizer recommended {recommendation['model_id']} "
                f"(cost=${recommendation['cost_per_mtok']:.2f}/MTok)"
            )
        else:
            logger.info("[MNEMOS] Optimizer failed, using default gpt-4o")
            model = "gpt-4o"

    logger.info(f"[MNEMOS] model={model}")

    # Build enhanced system prompt with MNEMOS context
    system_prompt = ""
    for msg in request.messages:
        if msg.role == "system":
            system_prompt = _content_text(msg.content)
            break

    if mnemos_docs:
        context_str = "\n\n".join([f"[Memory]\n{doc['content'][:500]}" for doc in mnemos_docs])
        system_prompt += f"\n\n[MNEMOS Context - {len(mnemos_docs)} memories]\n{context_str}"
        logger.info(f"[MNEMOS] Injected {len(mnemos_docs)} memories into context")

    # Prepare final messages for provider
    messages = []
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
        provider_stream = _route_to_provider_stream(
            model=model,
            messages=messages,
            generation_params=generation_params,
            request_params=request_params,
            user=user,
        )
        try:
            # Prime the upstream stream before constructing StreamingResponse.
            # This forces provider resolution, key/reliability checks, and the
            # initial upstream stream open/status check to fail while FastAPI can
            # still return a normal JSON error response instead of committing a
            # 200 SSE response that later truncates.
            first_delta = await anext(provider_stream)
        except StopAsyncIteration:
            first_delta = None
        except HTTPException as exc:
            if _is_openai_error_detail(exc.detail):
                return _openai_error_response(exc)
            raise
        except Exception as e:
            logger.error(f"[MNEMOS] Streaming request failed before response start: {e}")
            raise _stream_preflight_exception(e) from e

        async def event_source() -> AsyncIterator[str]:
            started_indexes: set[int] = set()
            finished_indexes: set[int] = set()
            try:
                if first_delta is not None:
                    for event in _stream_events_for_provider_delta(
                        delta=first_delta,
                        stream_id=stream_id,
                        created=now,
                        model=model,
                        started_indexes=started_indexes,
                        finished_indexes=finished_indexes,
                    ):
                        yield event

                async for delta in provider_stream:
                    for event in _stream_events_for_provider_delta(
                        delta=delta,
                        stream_id=stream_id,
                        created=now,
                        model=model,
                        started_indexes=started_indexes,
                        finished_indexes=finished_indexes,
                    ):
                        yield event
            except Exception as e:
                logger.error(f"[MNEMOS] Streaming response failed after response start: {e}")
                error_type = e.error_type if isinstance(e, ProviderStreamError) else "provider_stream_error"
                yield _stream_error_event(str(e), error_type=error_type)
            finally:
                try:
                    await provider_stream.aclose()
                except Exception as e:
                    logger.debug(f"[MNEMOS] Streaming response cleanup failed: {e}")
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_source(), media_type="text/event-stream")

    # Route to provider via GRAEAE
    try:
        provider_response = await _route_to_provider_response(
            model=model,
            messages=messages,
            generation_params=generation_params,
            request_params=request_params,
            user=user,
        )
    except HTTPException as exc:
        if _is_openai_error_detail(exc.detail):
            return _openai_error_response(exc)
        raise
    except Exception as e:
        logger.error(f"[MNEMOS] Request failed: {e}")
        raise HTTPException(status_code=503, detail=f"Request failed: {str(e)}")

    # Format OpenAI-compatible response
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
