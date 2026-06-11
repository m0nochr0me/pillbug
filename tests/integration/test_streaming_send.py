"""Streaming send path: delta forwarding, aggregation, and sticky non-streaming fallback.

When the loop passes a delta callback, GeminiChatSession tries `chat.send_message_stream`.
Upstreams that reject streamGenerateContent (the pillbug proxies return 501) disable
streaming for the rest of the runtime; other failures before the first emitted delta fall
back to a non-streaming send for that turn only. Failures after text reached the channel
re-raise so the loop's error handling takes over.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import types

from app.core import ai as ai_mod
from app.core.config import settings


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli")
    monkeypatch.setattr(settings, "CHANNEL_PLUGIN_FACTORIES", "")
    return settings


@pytest.fixture
def service(workspace_settings):
    return ai_mod.GeminiChatService()


def _chunk(*texts: str, thoughts: tuple[str, ...] = (), usage=None) -> SimpleNamespace:
    parts = [SimpleNamespace(thought=True, text=text) for text in thoughts]
    parts.extend(SimpleNamespace(thought=False, text=text) for text in texts)
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
        usage_metadata=usage,
    )


class _NotImplementedUpstreamError(Exception):
    """Mimics genai_errors.ServerError for a proxy's 501 streamGenerateContent stub."""

    def __init__(self) -> None:
        super().__init__("501 NOT_IMPLEMENTED")
        self.code = 501


class _FakeStreamingChat:
    def __init__(
        self,
        chunks: list[SimpleNamespace] | None = None,
        stream_error: Exception | None = None,
        fail_after_chunks: Exception | None = None,
        result: object = None,
    ) -> None:
        self._chunks = chunks or []
        self._stream_error = stream_error
        self._fail_after_chunks = fail_after_chunks
        self._result = result
        self.stream_calls = 0
        self.send_calls = 0

    async def send_message_stream(self, *, message, config):
        self.stream_calls += 1

        async def _generate():
            if self._stream_error is not None:
                raise self._stream_error
            for chunk in self._chunks:
                yield chunk
            if self._fail_after_chunks is not None:
                raise self._fail_after_chunks

        return _generate()

    async def send_message(self, *, message, config):
        self.send_calls += 1
        return self._result


def _collector(deltas: list[str]):
    async def on_text_delta(delta: str) -> None:
        deltas.append(delta)

    return on_text_delta


async def test_streaming_forwards_deltas_and_aggregates(service):
    session = service.create_session("cli:c1:u1")
    usage = types.GenerateContentResponseUsageMetadata(total_token_count=10)
    fake_chat = _FakeStreamingChat(
        chunks=[
            _chunk("Hel", thoughts=("planning...",)),
            _chunk("lo"),
            _chunk(usage=usage),
        ]
    )
    session._chat = fake_chat

    deltas: list[str] = []
    result = await session._send_chat_message(
        message="hi",
        config=SimpleNamespace(),
        on_text_delta=_collector(deltas),
    )

    # Thought parts are never emitted; only response text reaches the channel.
    assert deltas == ["Hel", "lo"]
    assert result.text == "Hello"
    assert result.usage_metadata is usage
    assert fake_chat.send_calls == 0


async def test_streaming_501_falls_back_and_disables_streaming(service):
    session = service.create_session("cli:c1:u1")
    sentinel = SimpleNamespace(text="non-streamed answer")
    fake_chat = _FakeStreamingChat(stream_error=_NotImplementedUpstreamError(), result=sentinel)
    session._chat = fake_chat

    deltas: list[str] = []
    result = await session._send_chat_message(
        message="hi",
        config=SimpleNamespace(),
        on_text_delta=_collector(deltas),
    )

    assert result is sentinel
    assert deltas == []
    assert service.streaming_disabled

    # The sticky flag makes the next turn skip the streaming attempt entirely.
    result_two = await session._send_chat_message(
        message="again",
        config=SimpleNamespace(),
        on_text_delta=_collector(deltas),
    )
    assert result_two is sentinel
    assert fake_chat.stream_calls == 1
    assert fake_chat.send_calls == 2


async def test_streaming_generic_failure_falls_back_without_disabling(service):
    session = service.create_session("cli:c1:u1")
    sentinel = SimpleNamespace(text="non-streamed answer")
    fake_chat = _FakeStreamingChat(stream_error=RuntimeError("transient upstream blip"), result=sentinel)
    session._chat = fake_chat

    result = await session._send_chat_message(
        message="hi",
        config=SimpleNamespace(),
        on_text_delta=_collector([]),
    )

    assert result is sentinel
    assert not service.streaming_disabled
    assert fake_chat.send_calls == 1


async def test_streaming_failure_after_output_reraises(service):
    session = service.create_session("cli:c1:u1")
    fake_chat = _FakeStreamingChat(
        chunks=[_chunk("He")],
        fail_after_chunks=RuntimeError("stream broke mid-turn"),
        result=SimpleNamespace(text="should not be used"),
    )
    session._chat = fake_chat

    deltas: list[str] = []
    with pytest.raises(RuntimeError, match="stream broke mid-turn"):
        await session._send_chat_message(
            message="hi",
            config=SimpleNamespace(),
            on_text_delta=_collector(deltas),
        )

    # Text already reached the user, so no silent non-streaming retry.
    assert deltas == ["He"]
    assert fake_chat.send_calls == 0


async def test_no_delta_callback_uses_non_streaming_send(service):
    session = service.create_session("cli:c1:u1")
    sentinel = SimpleNamespace(text="plain answer")
    fake_chat = _FakeStreamingChat(result=sentinel)
    session._chat = fake_chat

    result = await session._send_chat_message(message="hi", config=SimpleNamespace())

    assert result is sentinel
    assert fake_chat.stream_calls == 0
    assert fake_chat.send_calls == 1
