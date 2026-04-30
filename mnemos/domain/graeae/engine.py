#!/usr/bin/env python3
from __future__ import annotations

"""
GRAEAE Multi-Provider Consensus Engine

Queries multiple AI providers in parallel and returns all responses.

Provider registry
-----------------
Providers are declared in config.toml under [graeae.providers.<name>] — no
code changes needed to add or modify a provider. Keys are resolved from
~/.config/mnemos/api_keys.json (or $MNEMOS_KEYS_PATH). Built-in defaults
are used as a fallback when config.toml has no [graeae.providers] section.

API adapter styles (the "api" field in config.toml):
  "openai"    — OpenAI-compatible chat completions (Perplexity, Groq, xAI, OpenAI)
  "anthropic" — Anthropic Messages API
  "gemini"    — Google Gemini generateContent API

GPT-5 series is detected by model name ("gpt-5") and automatically uses
max_completion_tokens instead of max_tokens — no separate api_type needed.

Reliability stack (innermost to outermost):
  _concurrency     — asyncio.Semaphore per provider; sheds load when saturated
                     (Triton instance-slot model — skip, don't queue)
  _circuit_breaker — trips after 5 failures; recovers via HALF_OPEN probe
  _rate_limiter    — sliding-window RPM guard
  _quality         — rolling success-rate multiplier on base weight
  _cache           — in-memory LRU keyed on normalised prompt hash (1h TTL)
"""

import asyncio
import json
import logging
import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any, Dict, Optional

import httpx

from mnemos.core.config import get_settings
from mnemos.core.provider_registry import GRAEAE_REGISTRY_MAP
from mnemos.core.resilience import (
    call_maybe_async,
    make_circuit_breaker_pool,
    make_concurrency_limiter,
    make_rate_limiter_pool,
)
from mnemos.domain.graeae.api_keys import _PROVIDER_ENV_VARS, get_key


def _env_var_hint(key_name: str) -> str:
    """Return the env-var name an operator would export to bypass
    the Provider Registry File for a given key_name. Used in error
    messages so the hint is actionable."""
    return _PROVIDER_ENV_VARS.get(key_name, f"<{key_name.upper()}_API_KEY>")
from mnemos.domain.graeae._cache import ResponseCache
from mnemos.domain.graeae._quality import QualityTracker
from mnemos.domain.graeae.elo_sync import get_elo_weights

logger = logging.getLogger(__name__)


@dataclass
class ProviderResponse:
    provider: str
    status: str
    response_text: str
    latency_ms: int
    model_id: str
    final_score: float = 0.0


class ProviderStreamError(RuntimeError):
    """Structured provider error decoded from an SSE data frame."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str = "provider_error",
        status_code: int = 400,
        details: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.status_code = status_code
        self.details = details or {}


GEMINI_FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "OTHER": "stop",
    "FINISH_REASON_UNSPECIFIED": "stop",
}


def _normalize_gemini_finish_reason(raw: str | None) -> str:
    if raw is None:
        return "stop"
    raw_value = str(raw)
    canonical = {"stop", "length", "content_filter", "tool_calls"}
    if raw_value.lower() in canonical:
        return raw_value.lower()
    return GEMINI_FINISH_REASON_MAP.get(raw_value.upper(), "stop")



# Built-in provider defaults — used when config.toml has no [graeae.providers] section.
# Operators override these (or add new providers) via config.toml exclusively.
#
# Model IDs refreshed 2026-04-23 to current frontier (v3.1.2 Defect 3).
# Operators who want earlier generations override via config.toml.
# Defaults assume each provider's "flagship available to API" tier.
_BUILTIN_PROVIDERS: dict[str, dict] = {
    "together": {
        "url": "https://api.together.xyz/v1/chat/completions",
        "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "weight": 0.80, "api": "openai", "key_name": "together_ai",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "model": "llama-3.3-70b-versatile", "weight": 0.80, "api": "openai", "key_name": "groq",
    },
    "openai": {
        "url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-5.2-chat-latest", "weight": 0.88, "api": "openai", "key_name": "openai",
    },
    "claude": {
        "url": "https://api.anthropic.com/v1/messages",
        "model": "claude-opus-4-6", "weight": 0.90, "api": "anthropic", "key_name": "claude",
    },
    "perplexity": {
        "url": "https://api.perplexity.ai/chat/completions",
        "model": "sonar-pro", "weight": 0.88, "api": "openai", "key_name": "perplexity",
    },
    "xai": {
        "url": "https://api.x.ai/v1/chat/completions",
        "model": "grok-4-1-fast", "weight": 0.86, "api": "openai", "key_name": "xai",
    },
    "nvidia": {
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "model": "meta/llama-3.3-70b-instruct", "weight": 0.80, "api": "openai", "key_name": "nvidia",
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-preview:generateContent",
        "model": "gemini-3-pro-preview", "weight": 0.88, "api": "gemini", "key_name": "gemini",
    },
}


def _load_providers() -> dict[str, dict]:
    """Load provider registry from config.toml [graeae.providers].

    Falls back to _BUILTIN_PROVIDERS if the section is absent.
    Providers with enabled=false are excluded.
    The TOML 'api' field is kept as-is; dispatch in _query_provider() reads it.
    """
    try:
        from mnemos.core.config import GRAEAE_CONFIG
        registry = GRAEAE_CONFIG.get("providers", {})
    except ImportError:
        registry = {}

    if not registry:
        logger.debug("[GRAEAE] no providers in config.toml — using built-in defaults")
        return {k: dict(v) for k, v in _BUILTIN_PROVIDERS.items()}

    providers: dict[str, dict] = {}
    for name, cfg in registry.items():
        if not cfg.get("enabled", True):
            logger.info(f"[GRAEAE] provider '{name}' disabled in config.toml — skipping")
            continue
        required = {"url", "model", "weight", "api", "key_name"}
        missing = required - cfg.keys()
        if missing:
            logger.warning(f"[GRAEAE] provider '{name}' missing fields {missing} — skipping")
            continue
        providers[name] = dict(cfg)

    if not providers:
        logger.warning("[GRAEAE] config.toml [graeae.providers] is empty — using built-in defaults")
        return {k: dict(v) for k, v in _BUILTIN_PROVIDERS.items()}

    logger.info(f"[GRAEAE] loaded {len(providers)} providers from config.toml: {list(providers)}")
    return providers


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "image_url":
                image_url = block.get("image_url") or {}
                url = image_url.get("url") if isinstance(image_url, dict) else None
                if url:
                    parts.append(f"[image_url: {url}]")
        return "\n".join(p for p in parts if p)
    return str(content)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _provider_stream_error(data: dict[str, Any]) -> ProviderStreamError:
    raw_error = data.get("error")
    if isinstance(raw_error, dict):
        message = str(raw_error.get("message") or raw_error.get("detail") or "Provider stream error")
        error_type = str(raw_error.get("type") or raw_error.get("code") or "provider_error")
        raw_status = raw_error.get("status_code") or raw_error.get("status")
        try:
            status_code = int(raw_status) if raw_status is not None else 400
        except (TypeError, ValueError):
            status_code = 400
        if not 400 <= status_code <= 599:
            status_code = 400
        return ProviderStreamError(
            message,
            error_type=error_type,
            status_code=status_code,
            details=raw_error,
        )
    message = str(raw_error) if raw_error is not None else "Provider stream error"
    return ProviderStreamError(message, details={"error": raw_error})


def _openai_messages(messages: Optional[list[dict]], prompt: str) -> list[dict]:
    if messages:
        normalized: list[dict] = []
        for msg in messages:
            item = {k: v for k, v in msg.items() if v is not None}
            if "content" not in item and "tool_calls" not in item:
                item["content"] = ""
            normalized.append(item)
        return normalized
    return [{"role": "user", "content": prompt}]


def _anthropic_content_parts(content: Any) -> list[dict]:
    if isinstance(content, list):
        parts: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "image_url":
                image_url = block.get("image_url") or {}
                url = image_url.get("url") if isinstance(image_url, dict) else None
                if url:
                    parts.append({"type": "image", "source": {"type": "url", "url": url}})
        return parts or [{"type": "text", "text": ""}]
    return [{"type": "text", "text": _content_text(content)}]


def _anthropic_tool_input(arguments: Any) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {"arguments": arguments}
        return parsed if isinstance(parsed, dict) else {"arguments": parsed}
    return {}


def _anthropic_tool_use_blocks(tool_calls: Any) -> list[dict]:
    blocks: list[dict] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        name = function.get("name")
        call_id = call.get("id")
        if not name or not call_id:
            continue
        blocks.append({
            "type": "tool_use",
            "id": call_id,
            "name": name,
            "input": _anthropic_tool_input(function.get("arguments")),
        })
    return blocks


def _append_anthropic_message(messages: list[dict], role: str, content: list[dict]) -> None:
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(content)
        return
    messages.append({"role": role, "content": content})


def _anthropic_messages(messages: Optional[list[dict]], prompt: str) -> tuple[Optional[str], list[dict]]:
    if not messages:
        return None, [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

    system_parts: list[str] = []
    converted: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            text = _content_text(msg.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role == "assistant":
            content = (
                []
                if msg.get("content") is None and msg.get("tool_calls")
                else _anthropic_content_parts(msg.get("content"))
            )
            content.extend(_anthropic_tool_use_blocks(msg.get("tool_calls")))
            _append_anthropic_message(converted, "assistant", content)
            continue
        if role == "tool":
            tool_call_id = msg.get("tool_call_id")
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": _content_text(msg.get("content")),
                }
            ]
            _append_anthropic_message(converted, "user", content)
            continue
        _append_anthropic_message(converted, "user", _anthropic_content_parts(msg.get("content")))
    if not converted:
        converted.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
    return ("\n\n".join(system_parts) if system_parts else None), converted


def _anthropic_finish_reason(stop_reason: Any) -> str:
    if stop_reason == "tool_use":
        return "tool_calls"
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason == "end_turn":
        return "stop"
    return stop_reason or "stop"


def _anthropic_tools(tools: list[dict]) -> list[dict]:
    converted: list[dict] = []
    for tool in tools:
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            fn = tool["function"]
            converted.append({
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object"}),
            })
        else:
            converted.append(tool)
    return converted


def _anthropic_tool_choice(tool_choice: Any) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice in ("any", "required"):
            return {"type": "any"}
        if tool_choice == "none":
            return {"type": "none"}
        raise ValueError(f"unsupported Anthropic tool_choice {tool_choice!r}")
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        if tool_choice.get("type") == "function" and fn.get("name"):
            return {"type": "tool", "name": fn["name"]}
    raise ValueError(f"unsupported Anthropic tool_choice {tool_choice!r}")


def _gemini_part(content: Any) -> list[dict]:
    if isinstance(content, list):
        parts: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append({"text": block.get("text", "")})
            elif block.get("type") == "image_url":
                image_url = block.get("image_url") or {}
                url = image_url.get("url") if isinstance(image_url, dict) else None
                if url:
                    parts.append({"fileData": {"fileUri": url}})
        return parts or [{"text": ""}]
    return [{"text": _content_text(content)}]


def _gemini_contents(messages: Optional[list[dict]], prompt: str) -> tuple[Optional[dict], list[dict]]:
    if not messages:
        return None, [{"role": "user", "parts": [{"text": prompt}]}]

    system_text: list[str] = []
    contents: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            text = _content_text(msg.get("content"))
            if text:
                system_text.append(text)
            continue
        if role not in {"user", "assistant"}:
            raise ValueError(
                f"Gemini does not support role={role}; supported: system, user, assistant"
            )
        gemini_role = "model" if role == "assistant" else "user"
        contents.append({"role": gemini_role, "parts": _gemini_part(msg.get("content"))})
    if not contents:
        contents.append({"role": "user", "parts": [{"text": prompt}]})
    system_instruction = None
    if system_text:
        system_instruction = {"parts": [{"text": "\n\n".join(system_text)}]}
    return system_instruction, contents


class GraeaeEngine:
    """Multi-provider consensus reasoning engine."""

    def __init__(self):
        self.providers = _load_providers()
        self._client: Optional[httpx.AsyncClient] = None

        # Seed base weights from Arena.ai Elo leaderboard if available.
        # Uses on-disk cache — falls back to config.toml weights silently.
        elo = get_elo_weights(force_refresh=False)
        if elo:
            for name, w in elo.items():
                if name in self.providers:
                    self.providers[name]["weight"] = w
            logger.info(f"[GRAEAE] Elo weights applied for: {[p for p in elo if p in self.providers]}")

        # Reliability stack — instantiated here; _concurrency lazily initialised
        # on first consult() call because asyncio.Semaphore needs a running loop.
        self._settings = get_settings()
        self._circuit_breakers = make_circuit_breaker_pool(
            self._settings,
            failure_threshold=5,
            cooldown_seconds=300,
        )
        self._rate_limiters = make_rate_limiter_pool(self._settings)
        self._quality = QualityTracker({p: cfg["weight"] for p, cfg in self.providers.items()})
        self._cache = ResponseCache(ttl_seconds=3600, max_entries=500)
        self._concurrency: Optional[Any] = None

    def _get_concurrency(self) -> Any:
        """Lazy-init concurrency pool (requires running event loop)."""
        if self._concurrency is None:
            self._concurrency = make_concurrency_limiter(self._settings)
        return self._concurrency

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=200)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def reload_from_registry(self, pool) -> Dict[str, str]:
        """Refresh self.providers[*]['model'] from model_registry.

        Selection policy (per provider):
          1. ELO override: Arena-ranked top within the family is the default.
          2. Newer-version override: any same-family model with a strictly
             higher version tuple than the Arena top wins (catches fresh
             releases like claude-opus-4-7 over Arena-leading 4-6, or
             gpt-5.5 the day it ships ahead of Arena-ranked 5.4).
          3. Live probe + n-1 fallback: each candidate, in rank order, gets
             a tiny generate call; the first that returns 200 wins. If the
             top pick fails (model retired, regional rollout, key tier),
             we fall through to the next-best — usually the previous
             generation.

        Two-phase to avoid stalling startup or holding a pool connection
        for ~minutes on slow upstreams:
          Phase 1: pull every provider's candidate list in one DB conn,
                   then RELEASE the connection.
          Phase 2: probe candidates in parallel ACROSS providers (sequential
                   within a provider so n-1 fallback ordering is preserved),
                   no DB held.

        url/api/key_name/weight are preserved from the existing provider
        config — only the model field changes. gemini's URL embeds the
        model name and is rebuilt on rotation.
        """
        # Phase 1 — DB-bound, release the connection before any HTTP probe.
        plans: Dict[str, list[str]] = {}
        api_keys: Dict[str, str] = {}
        async with pool.acquire() as conn:
            for name, mapping in _REGISTRY_MAP.items():
                if name not in self.providers:
                    continue
                cfg = self.providers[name]
                api_key = get_key(cfg["key_name"])
                if not api_key:
                    logger.info("[GRAEAE] reload: %s → no api key, keeping %s",
                                name, cfg.get("model"))
                    continue
                candidates = await _ranked_candidates(
                    conn, mapping["registry_provider"], mapping["prefer"],
                )
                if not candidates:
                    logger.info("[GRAEAE] reload: %s → registry empty, keeping %s",
                                name, cfg.get("model"))
                    continue
                plans[name] = candidates[:_PROBE_MAX_CANDIDATES]
                api_keys[name] = api_key

        if not plans:
            logger.info("[GRAEAE] manifest reload: no eligible providers")
            return {}

        # Phase 2 — probe in parallel across providers (no DB held).
        client = await self._get_client()

        async def _probe_one_provider(name: str, cands: list[str], api_key: str):
            cfg = self.providers[name]
            tried: list[str] = []
            for cand in cands:
                if await _probe_model(client, cfg, cand, api_key):
                    return name, cand, tried
                tried.append(cand)
            return name, None, tried

        results = await asyncio.gather(
            *(_probe_one_provider(n, plans[n], api_keys[n]) for n in plans),
            return_exceptions=True,
        )

        changes: Dict[str, str] = {}
        for r in results:
            if isinstance(r, BaseException):
                logger.warning(f"[GRAEAE] probe task crashed: {type(r).__name__}: {r}")
                continue
            name, chosen, tried = r
            cfg = self.providers[name]
            if chosen is None:
                logger.warning(
                    "[GRAEAE] %s: all %d candidates failed probe (%s) — keeping %s",
                    name, len(tried), tried, cfg.get("model"),
                )
                continue
            if tried:
                logger.info(
                    "[GRAEAE] %s: probe failures %s → falling back to %s (n-%d)",
                    name, tried, chosen, len(tried),
                )
            old = cfg.get("model")
            if old == chosen:
                continue
            cfg["model"] = chosen
            if name == "gemini":
                cfg["url"] = (
                    f"https://generativelanguage.googleapis.com/v1beta/"
                    f"models/{chosen}:generateContent"
                )
            changes[name] = f"{old} → {chosen}"
        if changes:
            logger.info(f"[GRAEAE] manifest refreshed from registry: {changes}")
        else:
            logger.info("[GRAEAE] manifest reload: no changes (already current)")
        return changes

    async def consult(
        self,
        prompt: str,
        task_type: str = "reasoning",
        timeout: int = 180,
        selection: Optional[Dict[str, Optional[str]]] = None,
        mode: str = "auto",
    ) -> Dict:
        """Query eligible providers in parallel and return all responses.

        `selection` (v3.2 Custom Query mode) is an optional
        `{provider_name: model_override_or_None}` dict. When set, only
        those providers are considered for the fan-out; every other
        registered provider is omitted (not marked unavailable). A
        `model_override` value, if not None, overrides
        `self.providers[name]["model"]` for that one call.

        When `selection` is None, behavior is unchanged — every
        registered provider is considered (current auto-lineup).
        """
        task_type = task_type or "reasoning"
        mode = mode or "auto"
        if mode == "single":
            return await self.route_single(
                prompt, task_type, timeout=timeout, selection=selection,
            )
        if mode == "debate":
            return await self.route_debate(
                prompt, task_type, timeout=timeout, selection=selection,
            )
        if mode == "majority":
            return await self.route_majority(
                prompt, task_type, timeout=timeout, selection=selection,
            )
        if mode not in {"auto", "local", "external", "all"}:
            raise ValueError(f"unsupported consultation mode {mode!r}")

        # ── Cache check ──────────────────────────────────────────────────────
        # Include the selection (or lack thereof) in the cache key so a
        # Custom Query for "frontier only" doesn't get served the cached
        # all-providers response for the same prompt.
        cache_tag = _selection_cache_tag(selection) if selection else ""
        cache_key_task = f"{task_type}{cache_tag}"
        cached = self._cache.get(prompt, cache_key_task)
        if cached is not None:
            logger.info(f"[GRAEAE] cache hit (task_type={cache_key_task})")
            return {"all_responses": cached, "cache_hit": True, **_compute_consensus(cached)}

        concurrency = self._get_concurrency()

        # ── Selection-aware iteration list ───────────────────────────────────
        # If Custom Query set a lineup, respect it verbatim; unknown
        # provider names should have been rejected by the caller before
        # reaching the engine, but we guard defensively.
        if selection is not None:
            candidate_providers = [p for p in selection if p in self.providers]
        else:
            candidate_providers = list(self.providers)

        # ── Eligibility gate ─────────────────────────────────────────────────
        # A provider is skipped (not queued) if it is:
        #   • circuit-open (repeated recent failures)
        #   • rate-limited (RPM window exhausted)
        #   • saturated (all concurrency slots occupied)
        active: list[str] = []
        skipped: list[str] = []
        for name in candidate_providers:
            if not await call_maybe_async(self._circuit_breakers.is_allowed, name):
                skipped.append(name)
            elif not await call_maybe_async(self._rate_limiters.is_allowed, name):
                skipped.append(name)
            elif not await call_maybe_async(concurrency.acquire, name):
                skipped.append(name)
            else:
                active.append(name)

        if skipped:
            logger.info(f"[GRAEAE] skipped providers: {skipped}")

        if not active:
            logger.error("[GRAEAE] all providers unavailable")
            return {
                "all_responses": {
                    name: _unavailable(self.providers[name]["model"])
                    for name in candidate_providers
                },
                "error": "all providers unavailable",
            }

        # ── Fan-out ──────────────────────────────────────────────────────────
        # When a selection supplied a per-provider model override, pass it
        # through as a per-task argument instead of mutating self.providers.
        # In-place mutation would race with the background reload_from_registry
        # task: a concurrent reload that lands between save-and-restore would
        # be silently undone by the finally:'s restore step. Snapshot-by-arg
        # is also re-entrant for concurrent overlapping consult() calls.
        overrides: Dict[str, Optional[str]] = {}
        if selection is not None:
            for name, override in selection.items():
                if override and name in self.providers:
                    overrides[name] = override
        tasks = [
            asyncio.create_task(self._query_provider(
                name, prompt, task_type, timeout,
                model_override=overrides.get(name),
            ))
            for name in active
        ]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            for name in active:
                await call_maybe_async(concurrency.release, name)

        all_responses: Dict = {}

        for name, result in zip(active, results):
            if isinstance(result, Exception):
                await call_maybe_async(self._circuit_breakers.record_failure, name)
                self._quality.record_failure(name)
                err_msg = f"{type(result).__name__}: {str(result)[:400]}"
                logger.warning(f"[GRAEAE] muse {name} failed: {err_msg}")
                all_responses[name] = {
                    "status": "error",
                    "response_text": "",
                    "error": err_msg,
                    "latency_ms": 0,
                    "model_id": self.providers[name]["model"],
                    "final_score": 0.0,
                }
            else:
                await call_maybe_async(self._circuit_breakers.record_success, name)
                self._quality.record_success(name, result.get("latency_ms", 0))
                result["final_score"] = self._quality.dynamic_weight(name)
                all_responses[name] = result

        for name in skipped:
            all_responses[name] = _unavailable(self.providers[name]["model"])

        # ── Cache successful result ──────────────────────────────────────────
        if any(r["status"] == "success" for r in all_responses.values()):
            self._cache.set(prompt, cache_key_task, all_responses)

        # ── Compute consensus fields (v3.2) ──────────────────────────────────
        # ConsultationResponse has exposed consensus_response,
        # consensus_score, winning_muse, cost, latency_ms since v3.0
        # but the engine only emitted all_responses; consultation_id
        # callers saw all five as None. Compute them here from
        # all_responses so the contract is honored instead of
        # aspirational.
        consensus = _compute_consensus(all_responses)
        return {"all_responses": all_responses, **consensus}

    async def route_single(
        self,
        prompt: str,
        task_type: str = "reasoning",
        timeout: int = 180,
        selection: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict:
        """Run exactly one highest-weighted muse for the task."""
        lineup = await self._ranked_lineup(
            task_type=task_type, limit=1, selection=selection,
        )
        if not lineup:
            return {"all_responses": {}, "error": "no providers available", **_compute_consensus({})}
        return await self.consult(
            prompt, task_type, timeout=timeout, selection=lineup, mode="all",
        )

    async def route_debate(
        self,
        prompt: str,
        task_type: str = "reasoning",
        timeout: int = 180,
        rounds: int = 2,
        selection: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict:
        """Run a two-round cross-muse debate and return refined responses."""
        lineup = await self._ranked_lineup(
            task_type=task_type, limit=3, selection=selection,
        )
        if not lineup:
            empty = _compute_consensus({})
            return {
                "all_responses": {},
                "round_1": {},
                "round_2": {},
                "error": "no providers available",
                **empty,
            }

        round_1 = await self.consult(
            prompt, task_type, timeout=timeout, selection=lineup, mode="all",
        )
        round_1_responses = round_1.get("all_responses", {})
        if rounds < 2:
            return {
                "all_responses": round_1_responses,
                "round_1": round_1_responses,
                "round_2": {},
                **_compute_consensus(round_1_responses),
            }

        round_2_responses: Dict[str, Dict] = {}
        for provider_name, model_override in lineup.items():
            refine_prompt = _debate_refinement_prompt(
                prompt=prompt,
                current_muse=provider_name,
                round_1_responses=round_1_responses,
            )
            refined = await self.consult(
                refine_prompt,
                task_type,
                timeout=timeout,
                selection={provider_name: model_override},
                mode="all",
            )
            round_2_responses[provider_name] = (
                refined.get("all_responses", {}).get(provider_name)
                or _unavailable(self.providers[provider_name]["model"])
            )

        consensus = _compute_consensus(round_2_responses)
        return {
            "all_responses": round_2_responses,
            "round_1": round_1_responses,
            "round_2": round_2_responses,
            **consensus,
            "cost": float(round_1.get("cost", 0.0) or 0.0) + float(consensus.get("cost", 0.0) or 0.0),
            "latency_ms": int(round_1.get("latency_ms", 0) or 0) + int(consensus.get("latency_ms", 0) or 0),
        }

    async def route_majority(
        self,
        prompt: str,
        task_type: str = "reasoning",
        timeout: int = 180,
        quorum: float = 0.66,
        selection: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict:
        """Run up to three muses and report quorum agreement."""
        lineup = await self._ranked_lineup(
            task_type=task_type, limit=3, selection=selection,
        )
        result = await self.consult(
            prompt, task_type, timeout=timeout, selection=lineup or selection, mode="all",
        )
        all_responses = result.get("all_responses", {})
        quorum_result = _compute_quorum(all_responses, quorum)
        consensus = _compute_consensus(all_responses)
        consensus["consensus_score"] = quorum_result["consensus_score"]
        if quorum_result["quorum_reached"] and quorum_result["quorum_muses"]:
            winning_muse = max(
                quorum_result["quorum_muses"],
                key=lambda name: all_responses[name].get("final_score", 0.0),
            )
            consensus["winning_muse"] = winning_muse
            consensus["consensus_response"] = all_responses[winning_muse].get("response_text", "")

        return {
            **result,
            **consensus,
            "quorum_reached": quorum_result["quorum_reached"],
            "quorum_threshold": quorum,
            "similarity_pairs": quorum_result["similarity_pairs"],
        }

    async def _ranked_lineup(
        self,
        task_type: str,
        limit: int,
        selection: Optional[Dict[str, Optional[str]]] = None,
    ) -> Dict[str, Optional[str]]:
        """Pick the highest-weighted providers, preferring model_registry weights."""
        del task_type  # model_registry stores graeae_weight globally today.
        candidates = [
            name for name in (selection.keys() if selection is not None else self.providers.keys())
            if name in self.providers
        ]
        if not candidates:
            return {}

        ranked: list[tuple[str, Optional[str], float]] = []
        try:
            import mnemos.core.lifecycle as _lc

            if _lc._pool:
                registry_names = [
                    _REGISTRY_MAP.get(name, {}).get("registry_provider", name)
                    for name in candidates
                ]
                registry_to_graeae = {
                    cfg["registry_provider"]: name
                    for name, cfg in _REGISTRY_MAP.items()
                }
                async with _lc._pool.acquire() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT provider, model_id, graeae_weight
                        FROM model_registry
                        WHERE provider = ANY($1::text[])
                          AND available = true
                          AND deprecated = false
                        ORDER BY graeae_weight DESC NULLS LAST
                        """,
                        registry_names,
                    )
                seen: set[str] = set()
                for row in rows:
                    registry_provider = _row_get(row, "provider")
                    name = registry_to_graeae.get(registry_provider, registry_provider)
                    if name not in candidates or name in seen:
                        continue
                    model_override = selection.get(name) if selection else None
                    ranked.append((
                        name,
                        model_override or _row_get(row, "model_id"),
                        float(_row_get(row, "graeae_weight", self.providers[name].get("weight", 0.0)) or 0.0),
                    ))
                    seen.add(name)
        except Exception as exc:
            logger.debug("[GRAEAE] model_registry ranking unavailable: %s", exc)

        ranked_names = {name for name, _, _ in ranked}
        for name in candidates:
            if name in ranked_names:
                continue
            ranked.append((
                name,
                selection.get(name) if selection else None,
                float(self.providers[name].get("weight", 0.0) or 0.0),
            ))
        ranked.sort(key=lambda item: item[2], reverse=True)
        return {name: model_override for name, model_override, _ in ranked[:limit]}

    async def route(
        self,
        provider: str,
        model: str,
        prompt: str,
        task_type: str = "reasoning",
        timeout: int = 180,
        generation_params: Optional[Dict[str, Any]] = None,
        request_params: Optional[Dict[str, Any]] = None,
        messages: Optional[list[dict]] = None,
    ) -> Dict:
        """Single-provider pass-through — consensus skipped, eligibility
        gates applied.

        Used by MNEMOS gateway (`/v1/chat/completions`) for explicit
        model selection. Before v3.2 this path deliberately skipped
        the reliability stack "caller responsible for load management";
        operators pointed out that the gateway was effectively the
        weakest surface of the service because openai_compat did not
        actually implement any load management. v3.2 closes that gap:
        the circuit breaker, rate limiter, and concurrency guard are
        applied here exactly as they are in consult(), so one
        misbehaving provider can't take down the gateway while
        consultations keep working.

        Args:
            provider: Provider name (must exist in self.providers)
            model: Override model name (optional; uses provider config if None)
            prompt: Query text
            task_type: Task type for logging/tracking
            timeout: Request timeout in seconds
            generation_params: OpenAI generation controls (temperature,
                max_tokens, top_p) to map per provider
            request_params: OpenAI-compatible request fields already
                validated by the gateway (tools, response_format, stop, etc.)
            messages: Full chat messages to preserve system/history and
                multimodal content for adapters that support it

        Returns:
            Dict with status, response_text, latency_ms, model_id, error
        """
        if provider not in self.providers:
            logger.warning(f"[GRAEAE] unknown provider '{provider}' — returning unavailable")
            return _unavailable(
                model or provider,
                error=f"provider '{provider}' not registered in this deployment",
            )

        provider_config = dict(self.providers[provider])
        if model:
            provider_config["model"] = model

        # Key-missing is a common failure and silently produces a 401/403
        # upstream with no visible reason. Pre-check the key and emit a
        # targeted error so operators don't have to tail debug logs.
        api_key = get_key(provider_config["key_name"])
        if not api_key:
            logger.error(
                "[GRAEAE] route(%s) failed: missing api_key (key_name=%s) — "
                "set the %s environment variable or add the key to the "
                "Provider Registry File (MNEMOS_KEYS_PATH / "
                "~/.config/mnemos/api_keys.json)",
                provider,
                provider_config["key_name"],
                _env_var_hint(provider_config["key_name"]),
            )
            return _unavailable(
                provider_config["model"],
                error=(
                    f"missing api_key for provider '{provider}' "
                    f"(key_name={provider_config['key_name']})"
                ),
            )

        # v3.2 reliability gate: circuit-breaker → rate-limiter →
        # concurrency. Mirrors the consult() eligibility loop so
        # gateway traffic is first-class not second-class.
        if not await call_maybe_async(self._circuit_breakers.is_allowed, provider):
            logger.info("[GRAEAE] route(%s) refused: circuit open", provider)
            return _unavailable(
                provider_config["model"],
                error=f"provider '{provider}' circuit open",
            )
        if not await call_maybe_async(self._rate_limiters.is_allowed, provider):
            logger.info("[GRAEAE] route(%s) refused: rate limited", provider)
            return _unavailable(
                provider_config["model"],
                error=f"provider '{provider}' rate-limited",
            )
        concurrency = self._get_concurrency()
        if not await call_maybe_async(concurrency.acquire, provider):
            logger.info("[GRAEAE] route(%s) refused: concurrency saturated", provider)
            return _unavailable(
                provider_config["model"],
                error=f"provider '{provider}' concurrency saturated",
            )

        try:
            try:
                # Pass `model` through as model_override so the gateway's
                # per-call selection (e.g. /v1/chat/completions with
                # model="claude-opus-4-7") actually reaches dispatch
                # instead of being silently overwritten by whatever
                # self.providers[provider]["model"] currently holds.
                query_kwargs: Dict[str, Any] = {"model_override": model}
                if generation_params:
                    query_kwargs["generation_params"] = generation_params
                if request_params:
                    query_kwargs["request_params"] = request_params
                if messages is not None:
                    query_kwargs["messages"] = messages
                result = await self._query_provider(provider, prompt, task_type, timeout, **query_kwargs)
            except Exception as e:
                # Record the failure against the breaker so repeated
                # gateway-path failures actually trip it, and quality
                # tracker so the weight reflects reality.
                await call_maybe_async(self._circuit_breakers.record_failure, provider)
                self._quality.record_failure(provider)
                logger.error(f"[GRAEAE] route({provider}) failed: {e}")
                return _unavailable(
                    provider_config["model"],
                    error=f"{type(e).__name__}: {e}",
                )
            # Success path — credit the breaker + quality tracker so
            # the gateway's successes count toward reopening a
            # half-open circuit, not just consultations' successes.
            await call_maybe_async(self._circuit_breakers.record_success, provider)
            self._quality.record_success(provider, result.get("latency_ms", 0))
            logger.debug(
                f"[GRAEAE] route({provider}, {model or 'default'}) → {result['status']}"
            )
            return result
        finally:
            await call_maybe_async(concurrency.release, provider)

    async def route_stream(
        self,
        provider: str,
        model: str,
        prompt: str,
        task_type: str = "reasoning",
        timeout: int = 180,
        generation_params: Optional[Dict[str, Any]] = None,
        request_params: Optional[Dict[str, Any]] = None,
        messages: Optional[list[dict]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Single-provider streaming route.

        OpenAI-style providers use native SSE streaming. Other adapters fall
        back to one non-streaming provider call emitted as a single delta, so
        callers still get a faithful OpenAI SSE envelope without silently
        ignoring stream=True.
        """
        if provider not in self.providers:
            raise RuntimeError(f"provider '{provider}' not registered in this deployment")

        provider_config = dict(self.providers[provider])
        if model:
            provider_config["model"] = model

        api_key = get_key(provider_config["key_name"])
        if not api_key:
            raise RuntimeError(
                f"missing api_key for provider '{provider}' "
                f"(key_name={provider_config['key_name']})"
            )

        if not await call_maybe_async(self._circuit_breakers.is_allowed, provider):
            raise RuntimeError(f"provider '{provider}' circuit open")
        if not await call_maybe_async(self._rate_limiters.is_allowed, provider):
            raise RuntimeError(f"provider '{provider}' rate-limited")
        concurrency = self._get_concurrency()
        if not await call_maybe_async(concurrency.acquire, provider):
            raise RuntimeError(f"provider '{provider}' concurrency saturated")

        try:
            try:
                provider_cfg = dict(self.providers[provider])
                if model:
                    provider_cfg["model"] = model
                    if provider_cfg.get("api") == "gemini":
                        provider_cfg["url"] = (
                            f"https://generativelanguage.googleapis.com/v1beta/"
                            f"models/{model}:generateContent"
                        )

                if provider_cfg.get("api") == "openai":
                    async for chunk in self._stream_openai_compatible(
                        provider_cfg,
                        prompt,
                        timeout,
                        generation_params=generation_params,
                        request_params=request_params,
                        messages=messages,
                    ):
                        yield chunk
                else:
                    result = await self._query_provider(
                        provider,
                        prompt,
                        task_type,
                        timeout,
                        model_override=model,
                        generation_params=generation_params,
                        request_params=request_params,
                        messages=messages,
                    )
                    for choice in result.get("choices") or []:
                        msg = choice.get("message") or {}
                        index = choice.get("index", 0)
                        if msg.get("role"):
                            yield {"index": index, "role": msg["role"]}
                        content = msg.get("content")
                        if content:
                            yield {"index": index, "content": content}
                        if msg.get("tool_calls"):
                            yield {"index": index, "tool_calls": msg["tool_calls"]}
                        finish_reason = choice.get("finish_reason") or result.get("finish_reason") or "stop"
                        if provider_cfg.get("api") == "gemini":
                            finish_reason = _normalize_gemini_finish_reason(finish_reason)
                        yield {
                            "index": index,
                            "finish_reason": finish_reason,
                        }
                    if not result.get("choices"):
                        yield {"index": 0, "content": result.get("response_text", "")}
                        finish_reason = result.get("finish_reason") or "stop"
                        if provider_cfg.get("api") == "gemini":
                            finish_reason = _normalize_gemini_finish_reason(finish_reason)
                        yield {"index": 0, "finish_reason": finish_reason}
            except Exception:
                await call_maybe_async(self._circuit_breakers.record_failure, provider)
                self._quality.record_failure(provider)
                raise
            else:
                await call_maybe_async(self._circuit_breakers.record_success, provider)
                self._quality.record_success(provider, 0)
        finally:
            await call_maybe_async(concurrency.release, provider)

    async def _query_provider(
        self, provider_name: str, prompt: str, task_type: str, timeout: int,
        model_override: Optional[str] = None,
        generation_params: Optional[Dict[str, Any]] = None,
        request_params: Optional[Dict[str, Any]] = None,
        messages: Optional[list[dict]] = None,
    ) -> Dict:
        # Snapshot the provider config so a concurrent reload_from_registry
        # mutation can't tear the dict mid-dispatch. shallow copy is enough
        # because we only read scalar fields (model, url, weight, api,
        # key_name) and never mutate them here.
        provider = dict(self.providers[provider_name])
        if model_override:
            provider["model"] = model_override
            # gemini's URL embeds the model name; rebuild for the override.
            if provider.get("api") == "gemini":
                provider["url"] = (
                    f"https://generativelanguage.googleapis.com/v1beta/"
                    f"models/{model_override}:generateContent"
                )
        start = datetime.now(timezone.utc)
        api = provider["api"]

        if api == "openai":
            response = await self._query_openai_compatible(
                provider,
                prompt,
                timeout,
                generation_params=generation_params,
                request_params=request_params,
                messages=messages,
            )
        elif api == "anthropic":
            response = await self._query_anthropic(
                provider,
                prompt,
                timeout,
                generation_params=generation_params,
                request_params=request_params,
                messages=messages,
            )
        elif api == "gemini":
            response = await self._query_gemini(
                provider,
                prompt,
                timeout,
                generation_params=generation_params,
                request_params=request_params,
                messages=messages,
            )
        else:
            raise ValueError(f"Unknown api style '{api}' for provider '{provider_name}'")

        latency = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        response["latency_ms"] = latency
        response["final_score"] = provider["weight"]  # overridden by quality tracker in consult()
        return response

    def _openai_payload(
        self,
        provider: Dict,
        prompt: str,
        generation_params: Optional[Dict[str, Any]] = None,
        request_params: Optional[Dict[str, Any]] = None,
        messages: Optional[list[dict]] = None,
        stream: bool = False,
    ) -> Dict[str, Any]:
        generation_params = generation_params or {}
        request_params = request_params or {}

        is_gpt5 = provider["model"].startswith("gpt-5")
        tokens_key = "max_completion_tokens" if is_gpt5 else "max_tokens"
        payload: Dict[str, Any] = {
            "model": provider["model"],
            "messages": _openai_messages(messages, prompt),
            tokens_key: generation_params.get("max_tokens", 4096),
        }
        if "temperature" in generation_params:
            payload["temperature"] = generation_params["temperature"]
        elif not is_gpt5:
            payload["temperature"] = 0.7
        if "top_p" in generation_params:
            payload["top_p"] = generation_params["top_p"]
        for field in (
            "tools", "tool_choice", "response_format", "stop", "n",
            "presence_penalty", "frequency_penalty", "user",
        ):
            if field in request_params:
                payload[field] = request_params[field]
        if stream:
            payload["stream"] = True
        return payload

    async def _query_openai_compatible(
        self,
        provider: Dict,
        prompt: str,
        timeout: int,
        generation_params: Optional[Dict[str, Any]] = None,
        request_params: Optional[Dict[str, Any]] = None,
        messages: Optional[list[dict]] = None,
    ) -> Dict:
        """Query OpenAI-compatible APIs (Perplexity, Groq, xAI, OpenAI)."""
        api_key = get_key(provider["key_name"])
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = self._openai_payload(
            provider,
            prompt,
            generation_params=generation_params,
            request_params=request_params,
            messages=messages,
        )
        client = await self._get_client()
        resp = await client.post(provider["url"], json=payload, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"No choices in response: {data}")
        normalized_choices = []
        for i, choice in enumerate(choices):
            message = choice.get("message") or {}
            normalized_choices.append({
                "index": choice.get("index", i),
                "message": message,
                "finish_reason": choice.get("finish_reason") or "stop",
            })
        first_message = normalized_choices[0]["message"]
        return {
            "status": "success",
            "response_text": first_message.get("content") or "",
            "latency_ms": 0,
            "model_id": provider["model"],
            "choices": normalized_choices,
        }

    async def _stream_openai_compatible(
        self,
        provider: Dict,
        prompt: str,
        timeout: int,
        generation_params: Optional[Dict[str, Any]] = None,
        request_params: Optional[Dict[str, Any]] = None,
        messages: Optional[list[dict]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        api_key = get_key(provider["key_name"])
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = self._openai_payload(
            provider,
            prompt,
            generation_params=generation_params,
            request_params=request_params,
            messages=messages,
            stream=True,
        )
        client = await self._get_client()
        async with client.stream("POST", provider["url"], json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(f"HTTP {resp.status_code}: {body[:200]!r}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("[GRAEAE] skipped non-JSON stream frame: %s", raw[:120])
                    continue
                if isinstance(data, dict) and "error" in data:
                    raise _provider_stream_error(data)
                if not isinstance(data, dict):
                    logger.debug("[GRAEAE] skipped non-object stream frame: %s", raw[:120])
                    continue
                for choice in data.get("choices", []):
                    delta = choice.get("delta") or {}
                    chunk = {"index": choice.get("index", 0)}
                    if delta.get("role"):
                        chunk["role"] = delta["role"]
                    if delta.get("content") is not None:
                        chunk["content"] = delta["content"]
                    if delta.get("tool_calls"):
                        chunk["tool_calls"] = delta["tool_calls"]
                    if choice.get("finish_reason") is not None:
                        chunk["finish_reason"] = choice["finish_reason"]
                    if len(chunk) > 1:
                        yield chunk

    async def _query_anthropic(
        self,
        provider: Dict,
        prompt: str,
        timeout: int,
        generation_params: Optional[Dict[str, Any]] = None,
        request_params: Optional[Dict[str, Any]] = None,
        messages: Optional[list[dict]] = None,
    ) -> Dict:
        """Query Anthropic Claude API."""
        generation_params = generation_params or {}
        request_params = request_params or {}
        api_key = get_key(provider["key_name"])
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        system, anthropic_messages = _anthropic_messages(messages, prompt)
        payload = {
            "model": provider["model"],
            "max_tokens": generation_params.get("max_tokens", 4096),
            "messages": anthropic_messages,
        }
        if system:
            payload["system"] = system
        if "temperature" in generation_params:
            payload["temperature"] = generation_params["temperature"]
        if "top_p" in generation_params:
            payload["top_p"] = generation_params["top_p"]
        if "stop" in request_params:
            payload["stop_sequences"] = _as_list(request_params["stop"])
        if "tools" in request_params:
            payload["tools"] = _anthropic_tools(request_params["tools"])
        if "tool_choice" in request_params:
            payload["tool_choice"] = _anthropic_tool_choice(request_params["tool_choice"])
        client = await self._get_client()
        resp = await client.post(provider["url"], json=payload, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        content = data.get("content", [])
        text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
        tool_calls = []
        for block in content:
            if block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input", {}), separators=(",", ":")),
                    },
                })
        message: Dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "status": "success",
            "response_text": text,
            "latency_ms": 0,
            "model_id": provider["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": _anthropic_finish_reason(data.get("stop_reason"))}],
        }

    async def _query_gemini(
        self,
        provider: Dict,
        prompt: str,
        timeout: int,
        generation_params: Optional[Dict[str, Any]] = None,
        request_params: Optional[Dict[str, Any]] = None,
        messages: Optional[list[dict]] = None,
    ) -> Dict:
        """Query Google Gemini API."""
        generation_params = generation_params or {}
        request_params = request_params or {}
        api_key = get_key(provider["key_name"])
        headers = {"x-goog-api-key": api_key}
        system_instruction, contents = _gemini_contents(messages, prompt)
        generation_config: Dict[str, Any] = {
            "maxOutputTokens": generation_params.get("max_tokens", 4096),
            "temperature": generation_params.get("temperature", 0.7),
        }
        if "top_p" in generation_params:
            generation_config["topP"] = generation_params["top_p"]
        if "stop" in request_params:
            generation_config["stopSequences"] = _as_list(request_params["stop"])
        if request_params.get("n") not in (None, 1):
            generation_config["candidateCount"] = request_params["n"]
        if "presence_penalty" in request_params:
            generation_config["presencePenalty"] = request_params["presence_penalty"]
        if "frequency_penalty" in request_params:
            generation_config["frequencyPenalty"] = request_params["frequency_penalty"]
        response_format = request_params.get("response_format")
        if isinstance(response_format, dict) and response_format.get("type") == "json_object":
            generation_config["responseMimeType"] = "application/json"
        payload = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        client = await self._get_client()
        resp = await client.post(provider["url"], headers=headers, json=payload, timeout=timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError(f"No candidates in response: {data}")
        choices = []
        for i, candidate in enumerate(candidates):
            parts = candidate.get("content", {}).get("parts", [])
            text = "".join(part.get("text", "") for part in parts)
            choices.append({
                "index": i,
                "message": {"role": "assistant", "content": text},
                "finish_reason": _normalize_gemini_finish_reason(candidate.get("finishReason")),
            })
        text = choices[0]["message"]["content"] if choices else ""
        if not text:
            raise RuntimeError(f"Empty content in candidate: {candidates[0]}")
        return {
            "status": "success",
            "response_text": text,
            "latency_ms": 0,
            "model_id": provider["model"],
            "choices": choices,
        }

    def provider_status(self) -> Dict:
        """Circuit breaker, concurrency, rate limiter, quality, and cache stats."""
        status = {
            "circuit_breakers": self._circuit_breakers.status(),
            "rate_limiters": self._rate_limiters.status(),
            "quality": self._quality.status(),
            "cache": self._cache.stats(),
        }
        if self._concurrency:
            status["concurrency"] = self._concurrency.status()
        return status


def _compute_consensus(all_responses: Dict[str, Dict]) -> Dict:
    """Roll up per-provider responses into consensus fields.

    Emits:
      consensus_response — text of the highest-scoring successful
                           provider (winning muse). Empty string if
                           no provider succeeded.
      consensus_score    — the winner's final_score, or 0.0.
      winning_muse       — the provider name of the winner, or None.
      cost               — sum of per-provider `cost` fields (0.0
                           when a provider didn't report one). Matches
                           the consultation-persist path's existing
                           fallback that used the engine-reported cost
                           when present.
      latency_ms         — max latency across providers (parallel
                           fan-out: wall-clock to all_responses is
                           dominated by the slowest successful call).

    Contract: returns ALL keys even when there's no winner so callers
    never have to check for "field present" vs "field set". A
    no-winner consultation has consensus_response="", consensus_score=
    0.0, winning_muse=None, cost=0.0, latency_ms=0.
    """
    successes = [
        (name, resp)
        for name, resp in all_responses.items()
        if resp.get("status") == "success"
    ]
    if successes:
        winner_name, winner_resp = max(
            successes, key=lambda kv: kv[1].get("final_score", 0.0)
        )
    else:
        winner_name, winner_resp = None, None

    total_cost = 0.0
    for resp in all_responses.values():
        c = resp.get("cost")
        if isinstance(c, (int, float)):
            total_cost += float(c)

    latencies = [
        int(resp.get("latency_ms", 0) or 0)
        for resp in all_responses.values()
    ]
    max_latency = max(latencies) if latencies else 0

    return {
        "consensus_response": winner_resp.get("response_text", "") if winner_resp else "",
        "consensus_score": float(winner_resp.get("final_score", 0.0)) if winner_resp else 0.0,
        "winning_muse": winner_name,
        "cost": total_cost,
        "latency_ms": max_latency,
    }


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _debate_refinement_prompt(
    prompt: str,
    current_muse: str,
    round_1_responses: Dict[str, Dict],
) -> str:
    others: list[str] = []
    for name, response in round_1_responses.items():
        if name == current_muse:
            continue
        text = str(response.get("response_text") or "").strip()
        if text:
            others.append(f"{name}: {text}")
    context = "\n\n".join(others) if others else "No other round-1 responses were available."
    return (
        "Original consultation prompt:\n"
        f"{prompt}\n\n"
        "Round 1 responses from the other muses:\n"
        f"{context}\n\n"
        "Refine your answer. Address useful objections, keep what still holds, "
        "and be explicit where you disagree."
    )


def _text_similarity(left: str, right: str) -> float:
    left = " ".join(left.lower().split())
    right = " ".join(right.lower().split())
    if not left or not right:
        return 0.0
    return float(SequenceMatcher(None, left, right).ratio())


def _compute_quorum(all_responses: Dict[str, Dict], quorum: float) -> Dict[str, Any]:
    successes = {
        name: str(resp.get("response_text") or "").strip()
        for name, resp in all_responses.items()
        if resp.get("status") == "success" and str(resp.get("response_text") or "").strip()
    }
    if len(successes) < 2:
        return {
            "quorum_reached": False,
            "quorum_muses": [],
            "consensus_score": 0.0,
            "similarity_pairs": {},
        }

    parent = {name: name for name in successes}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    similarity_pairs: dict[str, float] = {}
    best_score = 0.0
    for left, right in combinations(successes, 2):
        score = _text_similarity(successes[left], successes[right])
        similarity_pairs[f"{left}:{right}"] = round(score, 4)
        best_score = max(best_score, score)
        if score >= quorum:
            union(left, right)

    components: dict[str, list[str]] = {}
    for name in successes:
        components.setdefault(find(name), []).append(name)
    quorum_size = max(2, math.ceil(quorum * len(successes)))
    quorum_muses = max(components.values(), key=len)
    quorum_reached = len(quorum_muses) >= quorum_size

    if quorum_reached and len(quorum_muses) > 1:
        component_scores = [
            similarity_pairs.get(f"{left}:{right}", similarity_pairs.get(f"{right}:{left}", 0.0))
            for left, right in combinations(quorum_muses, 2)
        ]
        consensus_score = max(component_scores) if component_scores else best_score
    else:
        consensus_score = min(best_score, max(0.0, quorum - 0.01))

    return {
        "quorum_reached": quorum_reached,
        "quorum_muses": quorum_muses if quorum_reached else [],
        "consensus_score": round(float(consensus_score), 4),
        "similarity_pairs": similarity_pairs,
    }


def _selection_cache_tag(selection: Optional[Dict[str, Optional[str]]]) -> str:
    """Deterministic string suffix for the response cache key when a
    Custom Query selection is active. Different lineups must not share
    a cache entry — two callers asking the same prompt under different
    lineups expect different result sets.
    """
    if not selection:
        return ""
    parts = sorted(
        f"{name}={override or ''}"
        for name, override in selection.items()
    )
    return "|" + ",".join(parts)


def _unavailable(model_id: str, error: str = "") -> Dict:
    """Uniform shape for provider failures.

    `error` (v3.1.2 diagnostic) is a short human-readable cause — e.g.
    "missing api_key", "HTTP 401 Unauthorized", "timeout after 30s" —
    surfaced by callers in logs and 503 responses so operators can
    diagnose without running the stack under DEBUG logging.
    """
    return {
        "status": "unavailable",
        "response_text": "",
        "latency_ms": 0,
        "model_id": model_id,
        "final_score": 0.0,
        "error": error,
    }


# ── Registry-backed manifest refresh ──────────────────────────────────────────
# Arena-ranked models always win over pattern matches when available.
_REGISTRY_MAP = GRAEAE_REGISTRY_MAP


_VERSION_RE = re.compile(r"(\d+)(?:[.\-_](\d+))?(?:[.\-_](\d+))?(?:[.\-_](\d+))?")
_VERSION_PAD = 4


def _extract_version(model_id: str) -> tuple:
    """Best-effort version tuple extraction for cherry-pick ordering.

    Captures the first dotted/hyphen-separated digit sequence, strips
    date-snapshot components (anything ≥ 1900 — years and YYYYMMDD
    stamps), and pads to a fixed length so prefix-shorter tuples don't
    outrank longer ones in ascending sort. Examples:

      gpt-5.4              → (5, 4, 0, 0)
      gpt-5                → (5, 0, 0, 0)
      gpt-5-2025-08-07     → (5, 0, 0, 0)   # 2025 = date, truncated
      claude-opus-4-7      → (4, 7, 0, 0)
      claude-haiku-4-5-20251001 → (4, 5, 0, 0)   # date stamp dropped
      gemini-3.1-pro-preview    → (3, 1, 0, 0)
      grok-4-0709          → (4, 709, 0, 0)

    After negation, gpt-5.4 sorts before gpt-5 in ascending order, so
    .sort() picks the highest version first.
    """
    m = _VERSION_RE.search(model_id)
    raw = [int(x) for x in m.groups() if x is not None] if m else []
    cleaned: list[int] = []
    for p in raw:
        # Anything ≥ 100 in a version slot is almost always a date code,
        # release stamp, or parameter count, not a major/minor version
        # — provider naming conventions don't ship versions like 5.235.
        # Stripping at 100 prevents 'grok-4-0709' (4, 709) from outranking
        # 'grok-4-1-fast' (4, 1) and 'gpt-5-2025-08-07' from outranking
        # 'gpt-5.4'.
        if p >= 100:
            break
        cleaned.append(p)
    cleaned.extend([0] * max(0, _VERSION_PAD - len(cleaned)))
    return tuple(cleaned[:_VERSION_PAD])


# How many candidates to probe before giving up on a provider. Each probe is
# one billable 1-token call, so we cap it. n-1 fallback covers "top pick is
# retired"; n-2 covers a double-rotation window. The cap is deliberately
# above _RISERS_MAX + arena_top + n-1 + n-2 so the Arena-ranked floor always
# gets a probe even when several post-Arena variants exist (e.g. four
# gpt-5.5 variants released the same day before Arena reviewed any).
_PROBE_MAX_CANDIDATES = 6
_RISERS_MAX = 3


def _is_reasoning_variant(model_id: str) -> bool:
    """True if ``model_id`` is a 'reasoning' SKU.

    Reasoning variants emit provider-specific reasoning blocks alongside
    the user-facing answer — e.g. xAI Grok-reasoning appends
    ``\\confidence{N}``; OpenAI o1 emits hidden chain-of-thought tokens
    that bleed through some adapters. The non-reasoning sibling
    (``...-non-reasoning`` or unsuffixed) returns clean text, which is
    what downstream consumers (consensus scoring, OpenAI-compat gateway,
    auto-context injection) expect. Prefer non-reasoning when both are
    available.
    """
    lower = model_id.lower()
    return lower.endswith("-reasoning") and not lower.endswith("-non-reasoning")


async def _ranked_candidates(conn, registry_provider: str, prefer: list[str]) -> list[str]:
    """Return model_ids ordered by selection priority (best first).

    Two-stage policy:
      Stage A (post-Arena risers): same-family models with a STRICTLY higher
        version tuple than the Arena-ranked top — covers fresh releases that
        Arena hasn't ranked yet.
      Stage B (Arena top + tier): the Arena-ranked top, then everything else
        in the same family tier by Arena score / version / last_synced.
      Stage C (other families): models matching later prefer-list entries
        (n-1 family) by the same internal ordering. Used as fallback when
        the primary family is unhealthy.

    Within each stage, ties break by version tuple DESC, arena_score DESC,
    last_synced DESC, **non-reasoning preferred over reasoning variants**,
    len(model_id) ASC (prefer canonical short names).

    The non-reasoning tiebreak was added in v4.1.2 after xAI split
    grok-4-1-fast into reasoning + non-reasoning SKUs with identical
    family/version/weight; the prior ``len()``-only tiebreak accidentally
    promoted ``grok-4.20-0309-reasoning`` (27 chars) over
    ``grok-4.20-0309-non-reasoning`` (31 chars), so every consultation
    came back tagged ``\\confidence{N}`` instead of clean text.
    """
    rows = await conn.fetch(
        """
        SELECT model_id, arena_score, last_synced
        FROM model_registry
        WHERE provider = $1 AND available AND NOT deprecated
        """,
        registry_provider,
    )
    if not rows:
        return []

    annotated: list[dict] = []
    for r in rows:
        mid = r["model_id"]
        family_rank: Optional[int] = None
        for i, pat in enumerate(prefer):
            if pat.lower() in mid.lower():
                family_rank = i
                break
        # Drop entries that match no family pattern AND have no Arena rank
        # (e.g. groq's whisper variants in the llama family slot).
        if family_rank is None and not r["arena_score"]:
            continue
        annotated.append({
            "mid": mid,
            "family_rank": family_rank if family_rank is not None else len(prefer),
            "version": _extract_version(mid),
            "arena": float(r["arena_score"] or 0),
            "synced": r["last_synced"].timestamp() if r["last_synced"] else 0,
        })
    if not annotated:
        return []

    def _internal_key(a: dict) -> tuple:
        return (
            a["family_rank"],
            tuple(-x for x in a["version"]),
            -a["arena"],
            -a["synced"],
            _is_reasoning_variant(a["mid"]),  # False (0) before True (1)
            len(a["mid"]),
        )

    # Identify the Arena top within the strongest family tier (lowest
    # family_rank that has any Arena entries).
    arena_top: Optional[dict] = None
    for tier in sorted({a["family_rank"] for a in annotated}):
        in_tier = [a for a in annotated if a["family_rank"] == tier and a["arena"] > 0]
        if in_tier:
            arena_top = max(in_tier, key=lambda a: a["arena"])
            break

    risers: list[dict] = []
    if arena_top is not None:
        risers = [
            a for a in annotated
            if a["family_rank"] == arena_top["family_rank"]
            and a["version"] > arena_top["version"]
            and a["mid"] != arena_top["mid"]
        ]
        risers.sort(key=_internal_key)
        # Cap risers so Arena-ranked floor isn't crowded out of the probe
        # budget on providers that ship many same-day variants of a new
        # version (e.g. four gpt-5.5 release variants).
        risers = risers[:_RISERS_MAX]

    riser_ids = {a["mid"] for a in risers}
    others = sorted(
        (a for a in annotated
         if (arena_top is None or a["mid"] != arena_top["mid"])
         and a["mid"] not in riser_ids),
        key=_internal_key,
    )

    ranked: list[str] = []
    seen: set[str] = set()
    for entry in risers + ([arena_top] if arena_top else []) + others:
        if entry is None:
            continue
        if entry["mid"] in seen:
            continue
        ranked.append(entry["mid"])
        seen.add(entry["mid"])
    return ranked


async def _probe_model(client: httpx.AsyncClient, provider_cfg: dict,
                       model_id: str, api_key: str, timeout: int = 15) -> bool:
    """Probe model_id with a tiny generate call; True iff HTTP 200.

    Probe bodies match the shape of _query_provider so a passing probe
    means the model can actually be dispatched against — not just that
    it appears in /v1/models. Token budgets are tuned per family:

      gpt-5.x reasoning models silently consume internal reasoning tokens
      from max_completion_tokens, so max_completion_tokens=1 always 400s
      with "max_tokens too low". Bumped to 128 to cover the reasoning
      phase + a 1-token output. Cost is pennies per startup × 8 providers.
    """
    api = provider_cfg["api"]
    try:
        if api == "openai":
            url = provider_cfg["url"]
            is_gpt5 = model_id.startswith("gpt-5")
            if is_gpt5:
                body = {
                    "model": model_id,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_completion_tokens": 128,
                }
            else:
                body = {
                    "model": model_id,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 4,
                    "temperature": 0,
                }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        elif api == "anthropic":
            url = provider_cfg["url"]
            body = {
                "model": model_id,
                "max_tokens": 4,
                "messages": [{"role": "user", "content": "hi"}],
            }
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        elif api == "gemini":
            url = (
                "https://generativelanguage.googleapis.com/v1beta/"
                f"models/{model_id}:generateContent"
            )
            body = {
                "contents": [{"parts": [{"text": "hi"}]}],
                "generationConfig": {"maxOutputTokens": 4, "temperature": 0},
            }
            headers = {"x-goog-api-key": api_key}
        else:
            return False
        resp = await client.post(url, json=body, headers=headers, timeout=timeout)
        return resp.status_code == 200
    except Exception as e:
        logger.debug(f"[GRAEAE] probe error for {model_id}: {type(e).__name__}: {e}")
        return False


# ── Module-level singleton ─────────────────────────────────────────────────────

_graeae_engine: Optional[GraeaeEngine] = None


def get_graeae_engine() -> GraeaeEngine:
    global _graeae_engine
    if _graeae_engine is None:
        _graeae_engine = GraeaeEngine()
    return _graeae_engine
