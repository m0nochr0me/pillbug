"""Gemini ⟷ OpenAI wire-format translation.

The Pillbug runtime drives Gemini's developer backend through google-genai,
which speaks `POST /v1beta/models/{model}:generateContent` with Gemini's
JSON shape. This module converts those payloads to OpenAI chat-completions
requests and converts OpenAI responses back into Gemini response bodies so
the SDK can consume them transparently.

Coverage matches what Pillbug actually sends today: text + inline image
parts, function declarations, function call/response parts,
`generationConfig.{temperature, topP, maxOutputTokens}`, and usage. Thinking
config and AFC config are dropped (AFC is a client-side loop). Non-image
inline payloads are replaced with a text marker.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

__all__ = (
    "StreamChunkAssembler",
    "gemini_request_to_openai",
    "openai_response_to_gemini",
)


_FINISH_REASON_MAP = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "STOP",
    "function_call": "STOP",
    "content_filter": "SAFETY",
}


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


def _system_instruction_text(payload: Any) -> str | None:
    if payload is None:
        return None
    if isinstance(payload, str):
        return payload.strip() or None
    if isinstance(payload, dict):
        parts = _pick(payload, "parts", default=[])
        if isinstance(parts, list):
            text = "".join(_coerce_text(part) for part in parts if isinstance(part, dict))
            return text.strip() or None
        return _coerce_text(payload).strip() or None
    return None


def _gemini_role_to_openai(role: str | None) -> str:
    if role == "model":
        return "assistant"
    if role in ("user", "assistant", "system", "tool"):
        return role
    return "user"


def _inline_part_to_content_block(inline_data: dict[str, Any]) -> dict[str, Any] | None:
    mime_type = _pick(inline_data, "mimeType", "mime_type")
    data = inline_data.get("data")
    if not isinstance(mime_type, str) or not isinstance(data, str):
        return None
    if mime_type.startswith("image/"):
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{data}"},
        }
    return {
        "type": "text",
        "text": f"[Attachment of type {mime_type} dropped: not supported by upstream OpenAI endpoint]",
    }


def _split_parts(parts: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (content_blocks, tool_calls_without_id, tool_responses)."""

    content_blocks: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    tool_responses: list[dict[str, Any]] = []

    for part in parts:
        if not isinstance(part, dict):
            continue

        text = part.get("text")
        if isinstance(text, str):
            content_blocks.append({"type": "text", "text": text})
            continue

        inline_data = _pick(part, "inlineData", "inline_data")
        if isinstance(inline_data, dict):
            block = _inline_part_to_content_block(inline_data)
            if block is not None:
                content_blocks.append(block)
            continue

        function_call = _pick(part, "functionCall", "function_call")
        if isinstance(function_call, dict):
            name = function_call.get("name", "")
            args = function_call.get("args") or {}
            tool_calls.append(
                {
                    "id": "",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            )
            continue

        function_response = _pick(part, "functionResponse", "function_response")
        if isinstance(function_response, dict):
            tool_responses.append(function_response)
            continue

        file_data = _pick(part, "fileData", "file_data")
        if isinstance(file_data, dict):
            content_blocks.append(
                {
                    "type": "text",
                    "text": "[File reference dropped: file uploads not supported by proxy backend]",
                }
            )
            continue

    return content_blocks, tool_calls, tool_responses


def _content_from_blocks(blocks: list[dict[str, Any]]) -> Any:
    if not blocks:
        return ""
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return blocks[0].get("text", "")
    return blocks


def _coerce_tool_response_text(response_payload: Any) -> str:
    if isinstance(response_payload, str):
        return response_payload
    return json.dumps(response_payload, ensure_ascii=False)


def gemini_request_to_openai(  # noqa: C901
    request_payload: dict[str, Any],
    *,
    model_override: str | None = None,
    default_model: str = "",
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []

    system_text = _system_instruction_text(_pick(request_payload, "systemInstruction", "system_instruction"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    contents = request_payload.get("contents") or []
    if isinstance(contents, dict):
        contents = [contents]

    pending_calls: list[dict[str, str]] = []
    call_counter = 0

    for entry in contents:
        if not isinstance(entry, dict):
            continue

        role = _gemini_role_to_openai(entry.get("role"))
        parts = entry.get("parts") or []
        if not isinstance(parts, list):
            continue

        content_blocks, tool_calls, tool_responses = _split_parts(parts)

        if role == "assistant":
            for call in tool_calls:
                call_counter += 1
                call["id"] = f"call_{call_counter}"
                pending_calls.append({"id": call["id"], "name": call["function"]["name"]})

            assistant_message: dict[str, Any] = {"role": "assistant"}
            content = _content_from_blocks(content_blocks)
            if tool_calls:
                assistant_message["content"] = content if content_blocks else None
                assistant_message["tool_calls"] = tool_calls
            else:
                assistant_message["content"] = content
            messages.append(assistant_message)
            continue

        for response in tool_responses:
            name = response.get("name", "")
            response_payload = response.get("response", response)
            matched_id: str | None = None
            for index, pending in enumerate(pending_calls):
                if pending["name"] == name:
                    matched_id = pending["id"]
                    pending_calls.pop(index)
                    break
            if matched_id is None:
                call_counter += 1
                matched_id = f"call_{call_counter}"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": matched_id,
                    "content": _coerce_tool_response_text(response_payload),
                }
            )

        if content_blocks:
            messages.append({"role": "user", "content": _content_from_blocks(content_blocks)})

    openai_request: dict[str, Any] = {
        "model": model_override or request_payload.get("model") or default_model,
        "messages": messages,
    }

    generation_config = _pick(request_payload, "generationConfig", "generation_config", default={})
    if isinstance(generation_config, dict):
        if (temperature := generation_config.get("temperature")) is not None:
            openai_request["temperature"] = temperature
        top_p = _pick(generation_config, "topP", "top_p")
        if top_p is not None:
            openai_request["top_p"] = top_p
        max_tokens = _pick(generation_config, "maxOutputTokens", "max_output_tokens")
        if max_tokens is not None:
            openai_request["max_tokens"] = max_tokens

    openai_tools: list[dict[str, Any]] = []
    for tool in request_payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        declarations = _pick(tool, "functionDeclarations", "function_declarations", default=[])
        if not isinstance(declarations, list):
            continue
        for declaration in declarations:
            if not isinstance(declaration, dict):
                continue
            openai_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": declaration.get("name", ""),
                        "description": declaration.get("description", ""),
                        "parameters": declaration.get("parameters") or {"type": "object", "properties": {}},
                    },
                }
            )
    if openai_tools:
        openai_request["tools"] = openai_tools

    return openai_request


def _openai_message_to_gemini_parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []

    content = message.get("content")
    if isinstance(content, str) and content:
        parts.append({"text": content})
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append({"text": block["text"]})

    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        name = function.get("name", "")
        arguments = function.get("arguments", "")
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            logger.warning(f"Tool call arguments were not valid JSON: {arguments!r}")
            args = {"_raw": arguments}
        parts.append({"functionCall": {"name": name, "args": args}})

    if not parts:
        parts.append({"text": ""})

    return parts


def openai_response_to_gemini(response_payload: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for choice in response_payload.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        finish_reason = (choice.get("finish_reason") or "stop").lower()
        candidates.append(
            {
                "content": {
                    "role": "model",
                    "parts": _openai_message_to_gemini_parts(message),
                },
                "finishReason": _FINISH_REASON_MAP.get(finish_reason, "STOP"),
                "index": choice.get("index", 0),
            }
        )

    if not candidates:
        candidates.append(
            {
                "content": {"role": "model", "parts": [{"text": ""}]},
                "finishReason": "STOP",
                "index": 0,
            }
        )

    usage = response_payload.get("usage") or {}
    usage_metadata = {
        "promptTokenCount": usage.get("prompt_tokens", 0) or 0,
        "candidatesTokenCount": usage.get("completion_tokens", 0) or 0,
        "totalTokenCount": usage.get("total_tokens", 0) or 0,
    }

    return {
        "candidates": candidates,
        "usageMetadata": usage_metadata,
        "modelVersion": response_payload.get("model", ""),
    }


def _parse_tool_args(arguments: str) -> dict[str, Any]:
    raw = arguments.strip()
    if not raw:
        return {}
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"Streamed tool call arguments were not valid JSON: {raw!r}")
        return {"_raw": raw}
    if not isinstance(args, dict):
        return {"_raw": args}
    return args


class StreamChunkAssembler:
    """Translate OpenAI chat-completions stream chunks into Gemini streamGenerateContent chunks.

    Chunk contract (google-genai's `chats._validate_response` marks the whole turn
    invalid otherwise, dropping it from curated history): every emitted chunk carries
    `candidates[0].content.parts` with at least one part, and `finishReason` plus
    `usageMetadata` ride on the last content-bearing chunk. Text deltas are therefore
    emitted with a one-chunk delay so the final delta can be merged with the finish
    metadata, and tool calls — whose argument JSON arrives as partial fragments — are
    buffered whole and emitted as `functionCall` parts when the stream finalizes.
    """

    def __init__(self) -> None:
        self._pending_text: str | None = None
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] = {}
        self._model_version = ""

    def handle_chunk(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []

        model = payload.get("model")
        if isinstance(model, str) and model:
            self._model_version = model

        usage = payload.get("usage")
        if isinstance(usage, dict):
            self._usage = usage

        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return []
        choice = choices[0]

        finish_reason = choice.get("finish_reason")
        if isinstance(finish_reason, str) and finish_reason:
            self._finish_reason = finish_reason

        delta = choice.get("delta") or {}
        if not isinstance(delta, dict):
            return []

        for tool_call in delta.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            index = tool_call.get("index", 0)
            index = index if isinstance(index, int) else 0
            bucket = self._tool_calls.setdefault(index, {"name": "", "argument_fragments": []})
            function = tool_call.get("function") or {}
            if isinstance(function, dict):
                name = function.get("name")
                if isinstance(name, str) and name:
                    bucket["name"] = name
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    bucket["argument_fragments"].append(arguments)

        content = delta.get("content")
        if isinstance(content, str) and content:
            previous, self._pending_text = self._pending_text, content
            if previous is not None:
                return [self._chunk([{"text": previous}])]

        return []

    def finalize(self) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        function_call_parts = [
            {"functionCall": {"name": bucket["name"], "args": _parse_tool_args("".join(bucket["argument_fragments"]))}}
            for _, bucket in sorted(self._tool_calls.items())
            if bucket["name"]
        ]

        if function_call_parts:
            if self._pending_text is not None:
                chunks.append(self._chunk([{"text": self._pending_text}]))
            chunks.append(self._chunk(function_call_parts, final=True))
        else:
            # Mirrors `openai_response_to_gemini`: an empty turn still produces one
            # `{"text": ""}` part so the finish metadata has content to ride on.
            chunks.append(self._chunk([{"text": self._pending_text or ""}], final=True))

        self._pending_text = None
        self._tool_calls = {}
        return chunks

    def _chunk(self, parts: list[dict[str, Any]], *, final: bool = False) -> dict[str, Any]:
        candidate: dict[str, Any] = {
            "content": {"role": "model", "parts": parts},
            "index": 0,
        }
        chunk: dict[str, Any] = {
            "candidates": [candidate],
            "modelVersion": self._model_version,
        }
        if final:
            finish_reason = (
                _FINISH_REASON_MAP.get(self._finish_reason.lower(), "STOP")
                if isinstance(self._finish_reason, str)
                else "STOP"
            )
            candidate["finishReason"] = finish_reason
            chunk["usageMetadata"] = {
                "promptTokenCount": self._usage.get("prompt_tokens", 0) or 0,
                "candidatesTokenCount": self._usage.get("completion_tokens", 0) or 0,
                "totalTokenCount": self._usage.get("total_tokens", 0) or 0,
            }
        return chunk
