"""genai-proxy streamGenerateContent endpoint: upstream SSE parsing and re-framing."""

from __future__ import annotations

import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("PB_GENAI_PROXY_UPSTREAM_URL", "http://127.0.0.1:9/v1")
app_module = pytest.importorskip("pillbug_genai_proxy.app")


def _upstream_sse_body() -> bytes:
    chunks = [
        {"model": "local-model", "choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]},
        {"model": "local-model", "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]},
        {"model": "local-model", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}},
    ]
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _parse_sse(body: str) -> list[dict]:
    return [json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")]


def test_stream_endpoint_reframes_upstream_sse(monkeypatch) -> None:
    captured_request: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_request["payload"] = json.loads(request.content)
        return httpx.Response(200, content=_upstream_sse_body(), headers={"content-type": "text/event-stream"})

    with TestClient(app_module.build_app()) as client:
        client.app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        response = client.post(
            "/v1beta/models/test-model:streamGenerateContent?alt=sse",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )

    assert response.status_code == 200
    assert captured_request["payload"]["stream"] is True
    assert captured_request["payload"]["stream_options"] == {"include_usage": True}

    chunks = _parse_sse(response.text)
    texts = ["".join(part.get("text", "") for part in c["candidates"][0]["content"]["parts"]) for c in chunks]
    assert texts == ["Hel", "lo"]
    assert chunks[-1]["candidates"][0]["finishReason"] == "STOP"
    assert chunks[-1]["usageMetadata"] == {
        "promptTokenCount": 8,
        "candidatesTokenCount": 2,
        "totalTokenCount": 10,
    }


def test_stream_endpoint_propagates_upstream_error_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="model loading")

    with TestClient(app_module.build_app()) as client:
        client.app.state.upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        response = client.post(
            "/v1beta/models/test-model:streamGenerateContent?alt=sse",
            json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == 503
