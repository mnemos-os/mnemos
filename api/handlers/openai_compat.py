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

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

import api.lifecycle as _lc
from api.auth import UserContext, get_current_user
from graeae.engine import get_graeae_engine, _REGISTRY_MAP

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


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: Optional[str] = "auto"
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = None
    top_p: Optional[float] = 1.0
    user: Optional[str] = None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatCompletionChoice]
    usage: Dict[str, int]


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
                from api.visibility import read_visibility_predicate
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


def _flatten_messages_for_prompt(messages: List[Dict[str, str]]) -> str:
    """Serialize a chat-completions ``messages`` array to a single prompt string.

    Used as a fallback when GRAEAE's single-provider route accepts only a
    flat prompt. Preserves role boundaries so a provider that was given a
    system prompt, prior assistant turns, and a fresh user question sees
    all three, not just the last user message (regression for #M31-02).
    """
    parts: List[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "") or ""
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


async def _route_to_provider(
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: Optional[int],
    user: UserContext,
) -> str:
    """Route request to selected provider via GRAEAE single-provider mode.

    Provider resolution: query model_registry for the exact model_id,
    falling back to substring heuristics only when the registry has no row.
    Unknown models are rejected with 400 instead of silently routing to a
    default.
    """
    graeae = get_graeae_engine()
    # Flatten the full messages array rather than keeping only
    # messages[-1]. The prior behaviour silently dropped the system prompt,
    # injected memory context, and every prior turn — multi-turn chat via
    # /v1/chat/completions collapsed to single-shot.
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
            status_code=400,
            detail=(
                f"unknown model {model!r}; not in model_registry and "
                f"no provider heuristic matched. Add the model to the "
                f"registry (update_model_registry.py) or use a known "
                f"model id."
            ),
        )

    logger.info(
        f"[MNEMOS] Route: model={model} → provider={provider} "
        f"(messages={len(messages)}, prompt_chars={len(prompt)})"
    )

    try:
        # Strip ONLY a leading `<provider>/` namespace that matches the
        # resolved provider — many upstream APIs use slash-bearing model
        # IDs natively (NVIDIA: `meta/llama-3.3-70b-instruct`, Together:
        # `meta-llama/Llama-3.3-70B-Instruct-Turbo`, `Qwen/Qwen3-235B-…`),
        # so a blanket "strip the first slash" rule would corrupt those.
        # Two equivalent prefixes are recognised:
        #   - GRAEAE name (e.g. `claude/`)
        #   - registry name (e.g. `anthropic/`) — what /v1/models surfaces
        # Only one is stripped; org-style prefixes the caller passes
        # through untouched (e.g. plain `meta-llama/Llama-…` is already
        # in the upstream API's expected shape).
        candidate_prefixes = [f"{provider}/"]
        if provider in _REGISTRY_MAP:
            registry_name = _REGISTRY_MAP[provider]["registry_provider"]
            if registry_name != provider:
                candidate_prefixes.append(f"{registry_name}/")
        bare_model = model
        for pfx in candidate_prefixes:
            if model.startswith(pfx):
                bare_model = model[len(pfx):]
                break
        # Use GRAEAE single-provider route (no consensus, just direct call)
        response = await graeae.route(provider, bare_model, prompt, task_type="reasoning", timeout=30)

        if response.get("status") == "success":
            return response.get("response_text", "")
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


# Defensive fallback for fresh installs whose model_registry hasn't
# been seeded yet. Matches the shape the old hardcoded list used; new
# deployments should run `update_model_registry.py` to populate real
# rows, but /v1/models stays usable in the meantime.
# Refreshed 2026-04-23 (v3.1.2 Defect 3) — aligned with the GRAEAE
# built-in provider defaults in graeae/engine.py._BUILTIN_PROVIDERS.
_FALLBACK_MODELS: list[dict] = [
    {"model_id": "gpt-5.2-chat-latest", "provider": "openai"},
    {"model_id": "claude-opus-4-6", "provider": "anthropic"},
    {"model_id": "gemini-3-pro-preview", "provider": "gemini"},
    {"model_id": "grok-4-1-fast", "provider": "xai"},
    {"model_id": "sonar-pro", "provider": "perplexity"},
    {"model_id": "llama-3.3-70b-versatile", "provider": "groq"},
]


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
    """List available models from the model_registry table (v3.1.2).

    Returns every row where available=true AND deprecated=false,
    ordered by graeae_weight DESC so higher-quality models lead the
    response. On a fresh install where the registry is empty (or the
    query fails), falls back to a short built-in list so the endpoint
    stays usable until `update_model_registry.py` seeds the table.
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
                "falling back to built-in list: %s", exc,
            )
            rows = []

    if not rows:
        rows = _FALLBACK_MODELS

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
    """Look up a single model in the registry (v3.1.2).

    Aliases resolve first (best-coding etc. → concrete model), then
    the resolved id is checked against model_registry. If the model
    isn't in the registry the handler still returns it with
    owned_by='Unknown' — this is a passthrough API and operators
    sometimes route to locally configured models that aren't
    registered globally.
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

    # Extract last user message for context search and task detection
    last_msg = ""
    for msg in reversed(request.messages):
        if msg.role == "user":
            last_msg = msg.content
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
            system_prompt = msg.content
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
                messages.append({"role": "system", "content": system_prompt})
                system_added = True
        else:
            messages.append({"role": msg.role, "content": msg.content})

    if not system_added and system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})

    # Route to provider via GRAEAE
    try:
        response_text = await _route_to_provider(
            model=model,
            messages=messages,
            temperature=request.temperature or 0.7,
            max_tokens=request.max_tokens,
            user=user,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[MNEMOS] Request failed: {e}")
        raise HTTPException(status_code=503, detail=f"Request failed: {str(e)}")

    # Format OpenAI-compatible response
    now = int(datetime.now(timezone.utc).timestamp())
    prompt_tokens = sum(len(m.get("content", "").split()) for m in messages)
    completion_tokens = len(response_text.split())

    return ChatCompletionResponse(
        id=f"chatcmpl-mnemos-{now}",
        created=now,
        model=model,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=response_text),
                finish_reason="stop",
            )
        ],
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    )
