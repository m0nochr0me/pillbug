"""streamGenerateContent endpoint: SSE framing and pre-stream error mapping."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

app_module = pytest.importorskip("pillbug_claude_api_proxy.app")


def _events() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(model="claude-test", usage=SimpleNamespace(input_tokens=4)),
        ),
        SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="Hel")),
        SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text="lo")),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=2),
        ),
        SimpleNamespace(type="message_stop"),
    ]


class _FakeEventStream:
    def __init__(self, events: list[SimpleNamespace]) -> None:
        self._events = iter(events)

    def __aiter__(self) -> _FakeEventStream:
        return self

    async def __anext__(self) -> SimpleNamespace:
        try:
            return next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None


def _request_body() -> dict:
    return {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}


def _parse_sse(body: str) -> list[dict]:
    return [json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")]


def test_stream_endpoint_emits_gemini_sse_chunks(monkeypatch) -> None:
    async def fake_start_inference_stream(**kwargs) -> _FakeEventStream:
        return _FakeEventStream(_events())

    monkeypatch.setattr(app_module, "start_inference_stream", fake_start_inference_stream)

    with TestClient(app_module.build_app()) as client:
        response = client.post("/v1beta/models/test-model:streamGenerateContent?alt=sse", json=_request_body())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    chunks = _parse_sse(response.text)
    texts = ["".join(part.get("text", "") for part in c["candidates"][0]["content"]["parts"]) for c in chunks]
    assert texts == ["Hel", "lo"]
    assert chunks[-1]["candidates"][0]["finishReason"] == "STOP"
    assert chunks[-1]["usageMetadata"] == {
        "promptTokenCount": 4,
        "candidatesTokenCount": 2,
        "totalTokenCount": 6,
    }


def test_stream_endpoint_maps_pre_stream_failure_to_502(monkeypatch) -> None:
    async def failing_start_inference_stream(**kwargs):
        raise RuntimeError("upstream auth failed")

    monkeypatch.setattr(app_module, "start_inference_stream", failing_start_inference_stream)

    with TestClient(app_module.build_app()) as client:
        response = client.post("/v1beta/models/test-model:streamGenerateContent?alt=sse", json=_request_body())

    assert response.status_code == 502
