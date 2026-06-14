"""Streaming send path: delta forwarding, aggregation, the manual AFC loop, and fallback.

When the loop passes a delta callback, GeminiChatSession streams via `chat.send_message_stream`
with the SDK's automatic function calling disabled, and drives tool rounds itself (google-genai
1.73.1's streaming AFC drops the tool call when a thinking model emits a content-less first
chunk). Upstreams that reject streamGenerateContent (the pillbug proxies return 501) disable
streaming for the rest of the runtime; other failures before any delta or tool side effect fall
back to a non-streaming send for that turn only. Failures after text reached the channel re-raise
so the loop's error handling takes over.
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


def _function_call_chunk(name: str, args: dict, usage=None) -> SimpleNamespace:
    part = SimpleNamespace(function_call=SimpleNamespace(name=name, args=args), thought=False, text=None)
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))],
        usage_metadata=usage,
    )


class _FakeAFCChat:
    """One `send_message_stream` call per tool round; a round may be chunks or an Exception."""

    def __init__(self, rounds: list[list[SimpleNamespace] | Exception]) -> None:
        self._rounds = rounds
        self.stream_calls = 0
        self.send_calls = 0
        self.configs: list[object] = []
        self.messages: list[object] = []

    async def send_message_stream(self, *, message, config):
        index = self.stream_calls
        self.stream_calls += 1
        self.configs.append(config)
        self.messages.append(message)
        round_item = self._rounds[index]

        async def _generate():
            if isinstance(round_item, Exception):
                raise round_item
            for chunk in round_item:
                yield chunk

        return _generate()

    async def send_message(self, *, message, config):
        self.send_calls += 1
        return SimpleNamespace(text="should not be used")


class _FakeMcpSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, *, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(isError=False)


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
        config=types.GenerateContentConfig(),
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
        config=types.GenerateContentConfig(),
        on_text_delta=_collector(deltas),
    )

    assert result is sentinel
    assert deltas == []
    assert service.streaming_disabled

    # The sticky flag makes the next turn skip the streaming attempt entirely.
    result_two = await session._send_chat_message(
        message="again",
        config=types.GenerateContentConfig(),
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
        config=types.GenerateContentConfig(),
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
            config=types.GenerateContentConfig(),
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

    result = await session._send_chat_message(message="hi", config=types.GenerateContentConfig())

    assert result is sentinel
    assert fake_chat.stream_calls == 0
    assert fake_chat.send_calls == 1


async def test_streaming_drives_function_calls_then_streams_final_text(service):
    # A thinking model that calls a tool would lose the call under the SDK's streaming AFC;
    # we run the tool ourselves and stream the model's final text on the next round.
    session = service.create_session("cli:c1:u1")
    usage = types.GenerateContentResponseUsageMetadata(total_token_count=5)
    fake_chat = _FakeAFCChat(
        rounds=[
            [_function_call_chunk("list_files", {"path": ".", "limit": 5.0})],
            [_chunk("All "), _chunk("done", usage=usage)],
        ]
    )
    session._chat = fake_chat

    fake_session = _FakeMcpSession()

    async def _fake_open():
        return SimpleNamespace(session=fake_session)

    session._ensure_mcp_client_open = _fake_open

    deltas: list[str] = []
    result = await session._send_chat_message(
        message="list then summarize",
        config=types.GenerateContentConfig(
            automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=3),
        ),
        on_text_delta=_collector(deltas),
    )

    assert deltas == ["All ", "done"]
    assert result.text == "All done"
    assert result.usage_metadata is usage
    # Two model rounds, no non-streaming fallback, and we executed the tool with a
    # whole-number float coerced back to int.
    assert fake_chat.stream_calls == 2
    assert fake_chat.send_calls == 0
    assert fake_session.calls == [("list_files", {"path": ".", "limit": 5})]
    # The SDK's own AFC is disabled on every streamed turn.
    assert all(cfg.automatic_function_calling.disable is True for cfg in fake_chat.configs)
    # Round 2's message is the function response we fed back.
    fed_back = fake_chat.messages[1]
    assert fed_back[0].function_response.name == "list_files"


async def test_streaming_tool_failure_after_first_round_reraises(service):
    # Once a tool has run, the chat history holds that turn; a later stream failure must
    # re-raise rather than silently re-sending the original message non-streaming.
    session = service.create_session("cli:c1:u1")
    fake_chat = _FakeAFCChat(
        rounds=[
            [_function_call_chunk("list_files", {})],
            RuntimeError("stream broke after tool round"),
        ]
    )
    session._chat = fake_chat

    async def _fake_open():
        return SimpleNamespace(session=_FakeMcpSession())

    session._ensure_mcp_client_open = _fake_open

    with pytest.raises(RuntimeError, match="stream broke after tool round"):
        await session._send_chat_message(
            message="hi",
            config=types.GenerateContentConfig(),
            on_text_delta=_collector([]),
        )
    assert fake_chat.send_calls == 0


async def test_execute_streamed_function_calls_wraps_results_and_errors(service):
    session = service.create_session("cli:c1:u1")

    class _Session:
        async def call_tool(self, *, name, arguments):
            if name == "boom":
                raise RuntimeError("nope")
            return SimpleNamespace(isError=(name == "bad"))

    async def _fake_open():
        return SimpleNamespace(session=_Session())

    session._ensure_mcp_client_open = _fake_open

    parts = await session._execute_streamed_function_calls(
        [
            types.FunctionCall(name="ok", args={"a": 1}),
            types.FunctionCall(name="bad", args={}),
            types.FunctionCall(name="boom", args={}),
        ]
    )

    assert [p.function_response.name for p in parts] == ["ok", "bad", "boom"]
    assert set(parts[0].function_response.response) == {"result"}
    assert set(parts[1].function_response.response) == {"error"}
    assert parts[2].function_response.response["error"] == "nope"
