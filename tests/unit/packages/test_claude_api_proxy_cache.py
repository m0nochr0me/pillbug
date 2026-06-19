"""Prompt-cache breakpoint injection and cache-token usage mapping for the claude-api proxy."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

translate = pytest.importorskip("pillbug_claude_api_proxy.translate")


def _count_breakpoints(request: dict) -> int:
    count = 0
    system = request.get("system")
    if isinstance(system, list):
        count += sum(1 for block in system if isinstance(block, dict) and "cache_control" in block)
    for message in request.get("messages", []):
        count += sum(1 for block in message.get("content", []) if isinstance(block, dict) and "cache_control" in block)
    return count


def test_breakpoints_cache_last_system_block_and_conversation_tail() -> None:
    request = {
        "system": [
            {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
            {"type": "text", "text": "stable user system prompt"},
        ],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ],
    }

    translate.apply_prompt_cache_breakpoints(request, ttl="5m")

    # The OAuth-validated identity prefix (first block) is untouched; the breakpoint lands on
    # the last system block, caching tools + the whole system prompt.
    assert "cache_control" not in request["system"][0]
    assert request["system"][1]["cache_control"] == {"type": "ephemeral"}
    # The conversation tail is cached; the earlier user turn is not (one breakpoint suffices here).
    assert request["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in request["messages"][0]["content"][0]
    assert _count_breakpoints(request) <= translate._MAX_CACHE_BREAKPOINTS


def test_breakpoints_skip_string_system_and_use_message_budget() -> None:
    # A bare-string system is the tiny identity prefix (sub-threshold); it cannot carry a
    # breakpoint, so all of the budget goes to the conversation.
    request = {
        "system": "You are Claude Code, Anthropic's official CLI for Claude.",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }

    translate.apply_prompt_cache_breakpoints(request, ttl="5m")

    assert isinstance(request["system"], str)
    assert request["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_breakpoints_use_extended_ttl() -> None:
    request = {"system": [{"type": "text", "text": "sys"}], "messages": []}

    translate.apply_prompt_cache_breakpoints(request, ttl="1h")

    assert request["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_breakpoints_cascade_over_long_turn_within_budget() -> None:
    # A single turn with many tool blocks exceeds the 20-block lookback window; cascaded
    # backstops keep the prior cache reachable. system uses one breakpoint, leaving three.
    blocks = [{"type": "text", "text": f"block-{i}"} for i in range(50)]
    request = {
        "system": [{"type": "text", "text": "sys"}],
        "messages": [{"role": "user", "content": blocks}],
    }

    translate.apply_prompt_cache_breakpoints(request, ttl="5m")

    cached_indices = sorted(i for i, block in enumerate(blocks) if "cache_control" in block)
    assert cached_indices == [9, 29, 49]  # anchored at the end (49), stepping back by 20
    assert _count_breakpoints(request) == translate._MAX_CACHE_BREAKPOINTS


def test_message_to_gemini_response_maps_cache_read_tokens() -> None:
    message = {
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "model": "claude-test",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 50,
        },
    }

    response = translate.message_to_gemini_response(message)

    # promptTokenCount is the full prompt (uncached + cache write + cache read); the cached
    # subset surfaces as cachedContentTokenCount.
    assert response["usageMetadata"] == {
        "promptTokenCount": 650,
        "candidatesTokenCount": 20,
        "totalTokenCount": 670,
        "cachedContentTokenCount": 500,
    }


def test_message_to_gemini_response_omits_cache_field_when_no_cache() -> None:
    message = {
        "content": [{"type": "text", "text": "hi"}],
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }

    response = translate.message_to_gemini_response(message)

    assert response["usageMetadata"] == {
        "promptTokenCount": 100,
        "candidatesTokenCount": 20,
        "totalTokenCount": 120,
    }
    assert "cachedContentTokenCount" not in response["usageMetadata"]


def test_stream_emits_cached_content_token_count() -> None:
    assembler = translate.StreamChunkAssembler()
    events = [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                model="claude-test",
                usage=SimpleNamespace(input_tokens=100, cache_read_input_tokens=500, cache_creation_input_tokens=50),
            ),
        ),
        SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="hi")),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=20),
        ),
        SimpleNamespace(type="message_stop"),
    ]

    chunks: list[dict] = []
    for event in events:
        chunks.extend(assembler.handle_event(event))

    assert chunks[-1]["usageMetadata"] == {
        "promptTokenCount": 650,
        "candidatesTokenCount": 20,
        "totalTokenCount": 670,
        "cachedContentTokenCount": 500,
    }
