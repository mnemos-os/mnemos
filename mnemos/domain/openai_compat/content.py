from typing import Any, Dict, List

from pydantic import BaseModel

from .schemas import ChatMessage


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
