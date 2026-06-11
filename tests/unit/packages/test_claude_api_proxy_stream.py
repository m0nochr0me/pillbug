"""StreamChunkAssembler: Anthropic stream events → Gemini streamGenerateContent chunks.

Contract under test: every emitted chunk has non-empty `candidates[0].content.parts`
(google-genai's `_validate_response` drops the whole turn from curated history
otherwise), and `finishReason` + `usageMetadata` ride on the last content-bearing
chunk — which forces the one-event delay on text deltas.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

translate = pytest.importorskip("pillbug_claude_api_proxy.translate")


def _message_start(model: str = "claude-test", input_tokens: int = 11) -> SimpleNamespace:
    return SimpleNamespace(
        type="message_start",
        message=SimpleNamespace(model=model, usage=SimpleNamespace(input_tokens=input_tokens)),
    )


def _text_delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        index=0,
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _tool_use_start(index: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_start",
        index=index,
        content_block=SimpleNamespace(type="tool_use", name=name, id=f"toolu_{index}"),
    )


def _tool_json_delta(index: int, partial_json: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        index=index,
        delta=SimpleNamespace(type="input_json_delta", partial_json=partial_json),
    )


def _block_stop(index: int) -> SimpleNamespace:
    return SimpleNamespace(type="content_block_stop", index=index)


def _message_delta(stop_reason: str = "end_turn", output_tokens: int = 7) -> SimpleNamespace:
    return SimpleNamespace(
        type="message_delta",
        delta=SimpleNamespace(stop_reason=stop_reason),
        usage=SimpleNamespace(output_tokens=output_tokens),
    )


def _message_stop() -> SimpleNamespace:
    return SimpleNamespace(type="message_stop")


def _drive(events: list[SimpleNamespace]) -> list[dict]:
    assembler = translate.StreamChunkAssembler()
    chunks: list[dict] = []
    for event in events:
        chunks.extend(assembler.handle_event(event))
    return chunks


def _parts(chunk: dict) -> list[dict]:
    return chunk["candidates"][0]["content"]["parts"]


def test_text_stream_delays_one_delta_and_merges_finish_metadata() -> None:
    chunks = _drive(
        [
            _message_start(),
            _text_delta("Hel"),
            _text_delta("lo "),
            _text_delta("world"),
            _message_delta(stop_reason="end_turn", output_tokens=3),
            _message_stop(),
        ]
    )

    assert [_parts(chunk) for chunk in chunks] == [
        [{"text": "Hel"}],
        [{"text": "lo "}],
        [{"text": "world"}],
    ]
    # Only the last chunk carries finish metadata; every chunk has content.
    assert all("finishReason" not in chunk["candidates"][0] for chunk in chunks[:-1])
    final = chunks[-1]
    assert final["candidates"][0]["finishReason"] == "STOP"
    assert final["usageMetadata"] == {
        "promptTokenCount": 11,
        "candidatesTokenCount": 3,
        "totalTokenCount": 14,
    }
    assert final["modelVersion"] == "claude-test"


def test_tool_use_is_buffered_into_final_function_call_chunk() -> None:
    chunks = _drive(
        [
            _message_start(),
            _text_delta("Let me check."),
            _tool_use_start(1, "read_file"),
            _tool_json_delta(1, '{"path": '),
            _tool_json_delta(1, '"notes.md"}'),
            _block_stop(1),
            _message_delta(stop_reason="tool_use"),
            _message_stop(),
        ]
    )

    assert [_parts(chunk) for chunk in chunks] == [
        [{"text": "Let me check."}],
        [{"functionCall": {"name": "read_file", "args": {"path": "notes.md"}}}],
    ]
    assert chunks[-1]["candidates"][0]["finishReason"] == "STOP"


def test_tool_use_with_empty_args_parses_to_empty_dict() -> None:
    chunks = _drive(
        [
            _message_start(),
            _tool_use_start(0, "list_files"),
            _block_stop(0),
            _message_delta(stop_reason="tool_use"),
            _message_stop(),
        ]
    )

    assert _parts(chunks[-1]) == [{"functionCall": {"name": "list_files", "args": {}}}]


def test_empty_message_still_produces_valid_final_chunk() -> None:
    chunks = _drive([_message_start(), _message_delta(), _message_stop()])

    assert len(chunks) == 1
    assert _parts(chunks[0]) == [{"text": ""}]
    assert chunks[0]["candidates"][0]["finishReason"] == "STOP"


def test_max_tokens_stop_reason_maps_to_max_tokens() -> None:
    chunks = _drive(
        [
            _message_start(),
            _text_delta("truncat"),
            _message_delta(stop_reason="max_tokens"),
            _message_stop(),
        ]
    )

    assert chunks[-1]["candidates"][0]["finishReason"] == "MAX_TOKENS"


def test_thinking_deltas_are_ignored() -> None:
    thinking = SimpleNamespace(
        type="content_block_delta",
        index=0,
        delta=SimpleNamespace(type="thinking_delta", thinking="hmm"),
    )
    chunks = _drive(
        [
            _message_start(),
            thinking,
            _text_delta("answer"),
            _message_delta(),
            _message_stop(),
        ]
    )

    assert [_parts(chunk) for chunk in chunks] == [[{"text": "answer"}]]
