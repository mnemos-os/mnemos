"""OpenAPI spec post-processing for downstream-target compatibility.

Some OpenAPI consumers enforce stricter field-length limits than
the spec itself does. The most-cited example is OpenAI's Custom
GPT Actions, which (per
https://developers.openai.com/api/docs/actions/production):

  * Endpoint ``summary`` / ``description`` truncate at 300 chars.
  * Parameter ``description`` truncates at 700 chars.

Long, prose-heavy FastAPI route docstrings hit those limits routinely
— a single ``GET /v1/memories/{id}`` description can run 800+
characters. Submitting that artifact to a Custom GPT either fails
import or silently truncates, neither of which surfaces the
problem to the operator at build time.

``truncate_for_gpt_actions`` walks the spec and rewrites the
relevant fields to fit the documented limits, so the artifact is
import-clean for the named target. Original spec is not mutated;
the function returns a new dict.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

# Limits per OpenAI Custom GPT Actions production docs.
GPT_ACTIONS_DESCRIPTION_LIMIT = 300
GPT_ACTIONS_PARAMETER_DESCRIPTION_LIMIT = 700

# Visual ellipsis marker so a truncated description reads as truncated
# in operator-facing tools rather than ending mid-sentence.
_ELLIPSIS = "…"


def _truncate(text: str, limit: int) -> str:
    """Cap ``text`` at ``limit`` chars, replacing the tail with a
    single ellipsis when truncation occurred. Treats limit < 1 as
    "drop the field" — return empty string."""
    if not isinstance(text, str):
        return text
    if limit < 1:
        return ""
    if len(text) <= limit:
        return text
    # Reserve one char for the ellipsis.
    return text[: limit - 1] + _ELLIPSIS


def _truncate_in_place(node: dict, key: str, limit: int) -> None:
    if key in node and isinstance(node[key], str):
        node[key] = _truncate(node[key], limit)


def truncate_for_gpt_actions(spec: Mapping[str, Any]) -> dict:
    """Return a copy of ``spec`` with endpoint descriptions and
    parameter descriptions capped at OpenAI Custom GPT Actions
    field-length limits.

    Walks every operation under ``paths.<path>.<method>``, capping:
      * ``summary`` and ``description`` at
        ``GPT_ACTIONS_DESCRIPTION_LIMIT`` (300 chars).
      * Each ``parameters[i].description`` at
        ``GPT_ACTIONS_PARAMETER_DESCRIPTION_LIMIT`` (700 chars).
      * Each ``requestBody.description`` at the same 700-char
        parameter limit (the docs treat the request body as a
        special parameter for length purposes).

    The schema body and response shapes are NOT touched — the
    Actions importer only enforces the description-class fields.
    Other OpenAPI consumers receiving the un-targeted spec keep
    the full descriptions.

    Tag-level descriptions are also capped at 300 chars since the
    Actions importer surfaces them in the same UI panel as
    operation summaries.
    """
    out = copy.deepcopy(dict(spec))

    paths = out.get("paths") or {}
    if isinstance(paths, dict):
        for _path, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method_key, op in methods.items():
                # Skip ``parameters`` and ``$ref`` siblings — they
                # aren't HTTP methods. The standard methods are
                # the openapi 3 verbs.
                if method_key.lower() not in {
                    "get", "put", "post", "delete", "options",
                    "head", "patch", "trace",
                }:
                    continue
                if not isinstance(op, dict):
                    continue
                _truncate_in_place(op, "summary", GPT_ACTIONS_DESCRIPTION_LIMIT)
                _truncate_in_place(op, "description", GPT_ACTIONS_DESCRIPTION_LIMIT)

                params = op.get("parameters")
                if isinstance(params, list):
                    for param in params:
                        if isinstance(param, dict):
                            _truncate_in_place(
                                param,
                                "description",
                                GPT_ACTIONS_PARAMETER_DESCRIPTION_LIMIT,
                            )

                request_body = op.get("requestBody")
                if isinstance(request_body, dict):
                    _truncate_in_place(
                        request_body,
                        "description",
                        GPT_ACTIONS_PARAMETER_DESCRIPTION_LIMIT,
                    )

    tags = out.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if isinstance(tag, dict):
                _truncate_in_place(tag, "description", GPT_ACTIONS_DESCRIPTION_LIMIT)

    return out


__all__ = [
    "truncate_for_gpt_actions",
    "GPT_ACTIONS_DESCRIPTION_LIMIT",
    "GPT_ACTIONS_PARAMETER_DESCRIPTION_LIMIT",
]
