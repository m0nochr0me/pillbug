"""Gemini ⟷ Anthropic wire-format translation for the direct-API proxy.

Pillbug speaks Gemini's developer wire format (`google-genai` → `POST
/v1beta/models/{model}:generateContent`). This module translates that
payload into Anthropic Messages API input — system prompt, message list
with `tool_use`/`tool_result` blocks, tool declarations, sampling params —
and translates the SDK's `Message` response back into a Gemini-shape
response body. Unlike the agent-loop sibling proxy, history flows through
verbatim as structured Anthropic blocks, so no transcript-in-system-prompt
bridge and no pseudo-XML scrubbing are needed.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = (
    "extract_system_text",
    "extract_history",
    "extract_tool_decls",
    "extract_generation_config",
    "message_to_gemini_response",
)


def _pick(payload: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload:
            return payload[name]
    return default


def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_coerce_text(item) for item in value)
    if isinstance(value, dict):
        return _coerce_text(_pick(value, "text", default=""))
    return ""


def extract_system_text(payload: dict[str, Any]) -> str | None:
    raw = _pick(payload, "systemInstruction", "system_instruction")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip() or None
    if isinstance(raw, dict):
        parts = _pick(raw, "parts", default=[])
        if isinstance(parts, list):
            text = "".join(_coerce_text(part) for part in parts if isinstance(part, dict))
            return text.strip() or None
        return _coerce_text(raw).strip() or None
    return None


def _gemini_role_to_anthropic(role: str | None) -> str:
    return "assistant" if role == "model" else "user"


def _inline_image_block(inline_data: dict[str, Any]) -> dict[str, Any] | None:
    mime_type = _pick(inline_data, "mimeType", "mime_type")
    data = inline_data.get("data")
    if not isinstance(mime_type, str) or not isinstance(data, str):
        return None
    if not mime_type.startswith("image/"):
        return {
            "type": "text",
            "text": f"[Attachment of type {mime_type} dropped: not supported by Claude API proxy]",
        }
    # google-genai serializes inline bytes with base64.urlsafe_b64encode ('-'/'_'),
    # but Anthropic's image API requires standard base64 ('+'/'/'). Transcode by
    # substituting the two differing alphabet characters; padding is identical.
    standard_b64 = data.replace("-", "+").replace("_", "/")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": mime_type, "data": standard_b64},
    }


def _parts_to_blocks(
    parts: list[Any], *, pending_calls: list[dict[str, str]], counter: list[int]
) -> list[dict[str, Any]]:
    """Convert Gemini parts of one content entry into Anthropic content blocks.

    `pending_calls` and `counter` are shared across the whole transcript so
    `functionResponse` parts can be linked back to the matching `functionCall`
    by name (Gemini lacks the call id Anthropic requires).
    """

    blocks: list[dict[str, Any]] = []

    for part in parts:
        if not isinstance(part, dict):
            continue

        text = part.get("text")
        if isinstance(text, str):
            blocks.append({"type": "text", "text": text})
            continue

        inline_data = _pick(part, "inlineData", "inline_data")
        if isinstance(inline_data, dict):
            block = _inline_image_block(inline_data)
            if block is not None:
                blocks.append(block)
            continue

        function_call = _pick(part, "functionCall", "function_call")
        if isinstance(function_call, dict):
            name = function_call.get("name", "")
            args = function_call.get("args") or {}
            counter[0] += 1
            tool_id = f"toolu_{counter[0]:08d}"
            pending_calls.append({"id": tool_id, "name": name})
            blocks.append({"type": "tool_use", "id": tool_id, "name": name, "input": args})
            continue

        function_response = _pick(part, "functionResponse", "function_response")
        if isinstance(function_response, dict):
            name = function_response.get("name", "")
            response_payload = function_response.get("response", function_response)
            matched_id: str | None = None
            for index, pending in enumerate(pending_calls):
                if pending["name"] == name:
                    matched_id = pending["id"]
                    pending_calls.pop(index)
                    break
            if matched_id is None:
                counter[0] += 1
                matched_id = f"toolu_{counter[0]:08d}"
            content_text = (
                response_payload
                if isinstance(response_payload, str)
                else json.dumps(response_payload, ensure_ascii=False)
            )
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": matched_id,
                    "content": [{"type": "text", "text": content_text}],
                }
            )
            continue

        file_data = _pick(part, "fileData", "file_data")
        if isinstance(file_data, dict):
            blocks.append(
                {
                    "type": "text",
                    "text": "[File reference dropped: file uploads not supported by Claude API proxy]",
                }
            )

    return blocks


def extract_history(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the conversation history as Anthropic-shaped messages."""

    contents = payload.get("contents") or []
    if isinstance(contents, dict):
        contents = [contents]

    pending_calls: list[dict[str, str]] = []
    counter = [0]
    messages: list[dict[str, Any]] = []

    for entry in contents:
        if not isinstance(entry, dict):
            continue
        parts = entry.get("parts") or []
        if not isinstance(parts, list):
            continue
        blocks = _parts_to_blocks(parts, pending_calls=pending_calls, counter=counter)
        if not blocks:
            continue
        messages.append({"role": _gemini_role_to_anthropic(entry.get("role")), "content": blocks})

    return _repair_tool_result_pairing(messages)


def _synthetic_tool_result(tool_use_id: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": [{"type": "text", "text": "[no tool result was recorded for this call]"}],
        "is_error": True,
    }


def _repair_tool_result_pairing(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Anthropic rejects any assistant `tool_use` whose id is not echoed back by
    # `tool_result` blocks in the very next user message. google-genai's AFC can
    # leave the Gemini history with an orphan `model: functionCall` when it
    # terminates mid-loop (e.g. maximum_remote_calls exceeded); after the next
    # user prompt is appended, that orphan reaches us as an unmatched tool_use.
    repaired: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        repaired.append(msg)
        if msg.get("role") != "assistant":
            i += 1
            continue

        tool_use_ids = [
            block["id"]
            for block in msg.get("content") or []
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
        ]
        if not tool_use_ids:
            i += 1
            continue

        next_msg = messages[i + 1] if i + 1 < len(messages) else None
        if next_msg is not None and next_msg.get("role") == "user":
            existing = next_msg.get("content") or []
            covered = {
                block["tool_use_id"]
                for block in existing
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")
            }
            missing = [tid for tid in tool_use_ids if tid not in covered]
            if missing:
                patched_content = [_synthetic_tool_result(tid) for tid in missing] + list(existing)
                repaired.append({"role": "user", "content": patched_content})
                i += 2
                continue
            i += 1
            continue

        repaired.append({"role": "user", "content": [_synthetic_tool_result(tid) for tid in tool_use_ids]})
        i += 1

    return repaired


_GEMINI_TYPE_TO_JSON_SCHEMA = {
    "STRING": "string",
    "NUMBER": "number",
    "INTEGER": "integer",
    "BOOLEAN": "boolean",
    "ARRAY": "array",
    "OBJECT": "object",
    "NULL": "null",
    "TYPE_UNSPECIFIED": "string",
}


def _normalize_schema(node: Any) -> Any:
    """Recursively normalize a Gemini schema dict into JSON Schema lowercase types.

    google-genai serializes `Schema.Type` enum values as uppercase strings
    (`"OBJECT"`, `"STRING"`, …) but Anthropic's tool `input_schema` validator
    requires lowercase JSON-Schema-style type names. We also drop Gemini-only
    keys that Anthropic rejects, and walk every container that can hold
    nested schemas.
    """

    if isinstance(node, list):
        return [_normalize_schema(item) for item in node]
    if not isinstance(node, dict):
        return node

    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key == "type" and isinstance(value, str):
            cleaned[key] = _GEMINI_TYPE_TO_JSON_SCHEMA.get(value, value.lower())
        elif key in {"properties", "patternProperties", "$defs", "definitions"} and isinstance(value, dict):
            cleaned[key] = {k: _normalize_schema(v) for k, v in value.items()}
        elif key in {"items", "additionalProperties", "not", "if", "then", "else"}:
            cleaned[key] = _normalize_schema(value)
        elif key in {"anyOf", "oneOf", "allOf", "prefixItems"} and isinstance(value, list):
            cleaned[key] = [_normalize_schema(item) for item in value]
        elif key in {"propertyOrdering", "nullable"}:
            # Gemini-only metadata Anthropic rejects.
            continue
        else:
            cleaned[key] = value
    return cleaned


def _coerce_tool_input_schema(parameters: Any) -> dict[str, Any]:
    """Ensure the tool input schema matches Anthropic's `{"type": "object", ...}` shape."""

    if not isinstance(parameters, dict) or not parameters:
        return {"type": "object", "properties": {}}
    normalized = _normalize_schema(parameters)
    if not isinstance(normalized, dict):
        return {"type": "object", "properties": {}}
    if normalized.get("type") != "object":
        normalized["type"] = "object"
    normalized.setdefault("properties", {})
    return normalized


def extract_tool_decls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert Gemini functionDeclarations into Anthropic tool definitions."""

    tools: list[dict[str, Any]] = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        declarations = _pick(tool, "functionDeclarations", "function_declarations", default=[])
        if not isinstance(declarations, list):
            continue
        for declaration in declarations:
            if not isinstance(declaration, dict):
                continue
            name = declaration.get("name")
            if not isinstance(name, str) or not name:
                continue
            description = declaration.get("description") or ""
            tools.append(
                {
                    "name": name,
                    "description": description,
                    "input_schema": _coerce_tool_input_schema(declaration.get("parameters")),
                }
            )
    return tools


def extract_generation_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Map Gemini generationConfig knobs onto Anthropic sampling params."""

    config = _pick(payload, "generationConfig", "generation_config", default={})
    if not isinstance(config, dict):
        return {}

    out: dict[str, Any] = {}
    temperature = config.get("temperature")
    if isinstance(temperature, int | float):
        out["temperature"] = float(temperature)

    top_p = _pick(config, "topP", "top_p")
    if isinstance(top_p, int | float):
        out["top_p"] = float(top_p)

    max_tokens = _pick(config, "maxOutputTokens", "max_output_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        out["max_tokens"] = max_tokens

    stop_sequences = _pick(config, "stopSequences", "stop_sequences")
    if isinstance(stop_sequences, list):
        cleaned = [s for s in stop_sequences if isinstance(s, str) and s]
        if cleaned:
            out["stop_sequences"] = cleaned

    return out


_FINISH_REASON_MAP = {
    "end_turn": "STOP",
    "stop_sequence": "STOP",
    "max_tokens": "MAX_TOKENS",
    "tool_use": "STOP",
}


def message_to_gemini_response(message: Any) -> dict[str, Any]:
    """Convert an anthropic SDK `Message` into a Gemini response payload.

    Accepts both pydantic SDK objects and already-shaped dicts.
    """

    content_blocks = _block_attr(message, "content", []) or []
    parts: list[dict[str, Any]] = []
    for block in content_blocks:
        btype = _block_type(block)
        if btype == "text":
            text = _block_attr(block, "text", "")
            if isinstance(text, str) and text:
                parts.append({"text": text})
        elif btype == "tool_use":
            name = _block_attr(block, "name", "")
            args = _block_attr(block, "input", {}) or {}
            if not isinstance(args, dict):
                args = {"_raw": args}
            parts.append({"functionCall": {"name": name, "args": args}})

    if not parts:
        parts.append({"text": ""})

    stop_reason = _block_attr(message, "stop_reason", None)
    finish_reason = _FINISH_REASON_MAP.get(stop_reason, "STOP") if isinstance(stop_reason, str) else "STOP"

    response: dict[str, Any] = {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": finish_reason,
                "index": 0,
            }
        ],
        "modelVersion": _block_attr(message, "model", "") or "",
    }

    usage = _block_attr(message, "usage", None)
    if usage is not None:
        input_tokens = _block_attr(usage, "input_tokens", 0) or 0
        output_tokens = _block_attr(usage, "output_tokens", 0) or 0
        response["usageMetadata"] = {
            "promptTokenCount": int(input_tokens),
            "candidatesTokenCount": int(output_tokens),
            "totalTokenCount": int(input_tokens) + int(output_tokens),
        }

    return response


_BLOCK_CLASS_NAME_TO_TYPE = {
    "TextBlock": "text",
    "ToolUseBlock": "tool_use",
    "ToolResultBlock": "tool_result",
    "ThinkingBlock": "thinking",
    "ImageBlock": "image",
}


def _block_type(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("type")
    explicit = getattr(block, "type", None)
    if isinstance(explicit, str):
        return explicit
    return _BLOCK_CLASS_NAME_TO_TYPE.get(type(block).__name__)


def _block_attr(block: Any, name: str, default: Any) -> Any:
    if isinstance(block, dict):
        return block.get(name, default)
    return getattr(block, name, default)
