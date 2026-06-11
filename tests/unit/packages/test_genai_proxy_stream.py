"""StreamChunkAssembler: OpenAI chat-completions stream chunks → Gemini chunks.

Same contract as the claude-api-proxy assembler: every emitted chunk has non-empty
`candidates[0].content.parts`, and `finishReason` + `usageMetadata` ride on the last
content-bearing chunk.
"""

from __future__ import annotations

import pytest

translate = pytest.importorskip("pillbug_genai_proxy.translate")


def _text_chunk(content: str, model: str = "local-model") -> dict:
    return {
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


def _tool_call_chunk(index: int, *, name: str | None = None, arguments: str | None = None) -> dict:
    function: dict = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    return {
        "choices": [
            {
                "index": 0,
                "delta": {"tool_calls": [{"index": index, "id": f"call_{index}", "function": function}]},
                "finish_reason": None,
            }
        ],
    }


def _finish_chunk(finish_reason: str) -> dict:
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}


def _usage_chunk(prompt: int, completion: int) -> dict:
    return {
        "choices": [],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
    }


def _drive(payloads: list[dict]) -> list[dict]:
    assembler = translate.StreamChunkAssembler()
    chunks: list[dict] = []
    for payload in payloads:
        chunks.extend(assembler.handle_chunk(payload))
    chunks.extend(assembler.finalize())
    return chunks


def _parts(chunk: dict) -> list[dict]:
    return chunk["candidates"][0]["content"]["parts"]


def test_text_stream_delays_one_delta_and_merges_finish_metadata() -> None:
    chunks = _drive(
        [
            _text_chunk("Hel"),
            _text_chunk("lo "),
            _text_chunk("world"),
            _finish_chunk("stop"),
            _usage_chunk(prompt=20, completion=5),
        ]
    )

    assert [_parts(chunk) for chunk in chunks] == [
        [{"text": "Hel"}],
        [{"text": "lo "}],
        [{"text": "world"}],
    ]
    assert all("finishReason" not in chunk["candidates"][0] for chunk in chunks[:-1])
    final = chunks[-1]
    assert final["candidates"][0]["finishReason"] == "STOP"
    assert final["usageMetadata"] == {
        "promptTokenCount": 20,
        "candidatesTokenCount": 5,
        "totalTokenCount": 25,
    }
    assert final["modelVersion"] == "local-model"


def test_tool_calls_are_buffered_into_final_function_call_chunk() -> None:
    chunks = _drive(
        [
            _text_chunk("Checking."),
            _tool_call_chunk(0, name="read_file", arguments='{"path": '),
            _tool_call_chunk(0, arguments='"notes.md"}'),
            _tool_call_chunk(1, name="list_files", arguments=""),
            _finish_chunk("tool_calls"),
        ]
    )

    assert [_parts(chunk) for chunk in chunks] == [
        [{"text": "Checking."}],
        [
            {"functionCall": {"name": "read_file", "args": {"path": "notes.md"}}},
            {"functionCall": {"name": "list_files", "args": {}}},
        ],
    ]
    assert chunks[-1]["candidates"][0]["finishReason"] == "STOP"


def test_empty_stream_still_produces_valid_final_chunk() -> None:
    chunks = _drive([])

    assert len(chunks) == 1
    assert _parts(chunks[0]) == [{"text": ""}]
    assert chunks[0]["candidates"][0]["finishReason"] == "STOP"
    assert chunks[0]["usageMetadata"]["totalTokenCount"] == 0


def test_length_finish_reason_maps_to_max_tokens() -> None:
    chunks = _drive([_text_chunk("truncat"), _finish_chunk("length")])

    assert chunks[-1]["candidates"][0]["finishReason"] == "MAX_TOKENS"


def test_malformed_tool_arguments_fall_back_to_raw() -> None:
    chunks = _drive(
        [
            _tool_call_chunk(0, name="search", arguments='{"q": broken'),
            _finish_chunk("tool_calls"),
        ]
    )

    assert _parts(chunks[-1]) == [{"functionCall": {"name": "search", "args": {"_raw": '{"q": broken'}}}]
