"""Upstream adapter that calls anthropic.AsyncAnthropic.messages.create.

One Gemini `:generateContent` request is one synchronous Anthropic Messages
API call. The conversation history (including prior `tool_use` / `tool_result`
round-trips) reaches the model as native structured blocks; there is no
agent loop, no MCP stub layer, and no transcript-in-system-prompt bridge.
This is the whole reason the variant exists: without an agent loop priming
the model on a transcript dialect, the pseudo-XML leakage that plagues the
sibling proxy cannot occur.

Auth: Bearer OAuth token from `claude setup-token` (`CLAUDE_CODE_OAUTH_TOKEN`).
The Claude subscription path also requires an `anthropic-beta` header and a
Claude-Code identity prefix on the system prompt; both are configurable.
"""

from __future__ import annotations

import asyncio
from typing import Any

from anthropic import AsyncAnthropic
from loguru import logger

from pillbug_claude_api_proxy import translate
from pillbug_claude_api_proxy.config import settings

__all__ = ("run_inference", "start_inference_stream")


_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is not None:
        return _client

    token = settings.resolved_oauth_token()
    if not token:
        raise RuntimeError(
            "no OAuth token available: set PB_CLAUDE_API_PROXY_OAUTH_TOKEN or CLAUDE_CODE_OAUTH_TOKEN "
            "(run `claude setup-token` to mint one)"
        )

    default_headers: dict[str, str] = {}
    if settings.OAUTH_BETA_HEADER:
        default_headers["anthropic-beta"] = settings.OAUTH_BETA_HEADER

    _client = AsyncAnthropic(
        auth_token=token,
        default_headers=default_headers,
        timeout=settings.REQUEST_TIMEOUT_SECONDS,
    )
    return _client


def _compose_system_prompt(system_text: str | None) -> list[dict[str, str]] | str | None:
    """Build the Anthropic `system` field.

    The Claude Code OAuth subscription path validates the FIRST system
    content block against an exact-match Claude-Code identity prefix.
    Concatenating the user's systemInstruction into the same string
    breaks that check (HTTP 429 rate_limit_error), so when both are
    present we send a two-block array. When only one is present we send
    a plain string — same as what worked in isolation.
    """

    prefix = (settings.CLAUDE_CODE_SYSTEM_PREFIX or "").strip()
    body = (system_text or "").strip()
    if prefix and body:
        return [
            {"type": "text", "text": prefix},
            {"type": "text", "text": body},
        ]
    if prefix:
        return prefix
    if body:
        return body
    return None


def _build_request_kwargs(
    *,
    system_text: str | None,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    generation_config: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    target_model = settings.MODEL or model
    if not target_model:
        raise RuntimeError("no model configured: set PB_CLAUDE_API_PROXY_MODEL or include it in the request URL")

    system_prompt = _compose_system_prompt(system_text)

    max_tokens = generation_config.get("max_tokens") or settings.MAX_TOKENS

    request_kwargs: dict[str, Any] = {
        "model": target_model,
        "max_tokens": max_tokens,
        "messages": history,
    }
    if system_prompt is not None:
        request_kwargs["system"] = system_prompt
    if tools:
        request_kwargs["tools"] = tools
    if "temperature" in generation_config:
        request_kwargs["temperature"] = generation_config["temperature"]
    if "top_p" in generation_config:
        request_kwargs["top_p"] = generation_config["top_p"]
    if "stop_sequences" in generation_config:
        request_kwargs["stop_sequences"] = generation_config["stop_sequences"]

    return request_kwargs


async def run_inference(
    *,
    system_text: str | None,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    generation_config: dict[str, Any],
    model: str,
) -> dict[str, Any]:
    """Run one Anthropic Messages call and return a Gemini-shape response payload."""

    client = _get_client()
    request_kwargs = _build_request_kwargs(
        system_text=system_text,
        history=history,
        tools=tools,
        generation_config=generation_config,
        model=model,
    )

    try:
        message = await asyncio.wait_for(
            client.messages.create(**request_kwargs),
            timeout=settings.REQUEST_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        logger.warning(f"Anthropic API call timed out after {settings.REQUEST_TIMEOUT_SECONDS}s")
        raise RuntimeError("upstream Anthropic call timed out") from exc

    logger.debug(
        f"Anthropic turn returned: stop_reason={getattr(message, 'stop_reason', None)!r} "
        f"block_types={[type(b).__name__ for b in (message.content or [])]} "
        f"usage_present={bool(getattr(message, 'usage', None))}"
    )

    return translate.message_to_gemini_response(message)


async def start_inference_stream(
    *,
    system_text: str | None,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    generation_config: dict[str, Any],
    model: str,
) -> Any:
    """Open a streaming Anthropic Messages call and return the raw event stream.

    Awaiting the SDK call sends the request, so upstream failures (auth, bad
    request, overload) raise here — before the proxy commits to an SSE response —
    and surface to the client as a normal HTTP error.
    """

    client = _get_client()
    request_kwargs = _build_request_kwargs(
        system_text=system_text,
        history=history,
        tools=tools,
        generation_config=generation_config,
        model=model,
    )
    return await client.messages.create(**request_kwargs, stream=True)
