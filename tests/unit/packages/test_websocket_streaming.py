"""WebSocket channel stream_response: `stream` delta events plus a terminal `message` event."""

from __future__ import annotations

import os

import pytest

from app.schema.messages import InboundMessage

os.environ.setdefault("PB_WEBSOCKET_BEARER_TOKEN", "test-token")
websocket_channel = pytest.importorskip("pillbug_websocket.websocket_channel")

WebSocketChannel = websocket_channel.WebSocketChannel

_SESSION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


class _FakeSio:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict, str]] = []

    async def emit(self, event: str, payload: dict, to: str | None = None) -> None:
        self.emitted.append((event, payload, to))


def _connected_channel() -> tuple[WebSocketChannel, _FakeSio]:
    channel = WebSocketChannel()
    fake_sio = _FakeSio()
    channel._sio = fake_sio
    channel._session_to_sids[_SESSION_ID] = {"sid-1"}
    channel._sid_to_session["sid-1"] = _SESSION_ID
    return channel, fake_sio


def _inbound(conversation_id: str = _SESSION_ID) -> InboundMessage:
    return InboundMessage(
        channel_name="websocket",
        conversation_id=conversation_id,
        user_id=f"ws:{conversation_id}",
        text="hi",
    )


async def test_stream_emits_deltas_then_terminal_message():
    channel, fake_sio = _connected_channel()

    async with channel.stream_response(_inbound()) as emit:
        await emit("Hel")
        await emit("lo")

    assert fake_sio.emitted == [
        ("stream", {"session_id": _SESSION_ID, "delta": "Hel"}, "sid-1"),
        ("stream", {"session_id": _SESSION_ID, "delta": "lo"}, "sid-1"),
        ("message", {"session_id": _SESSION_ID, "text": "Hello"}, "sid-1"),
    ]


async def test_stream_without_output_sends_no_terminal_message():
    channel, fake_sio = _connected_channel()

    async with channel.stream_response(_inbound()):
        pass

    assert fake_sio.emitted == []


async def test_stream_exception_skips_terminal_message():
    channel, fake_sio = _connected_channel()

    with pytest.raises(RuntimeError, match="turn failed"):
        async with channel.stream_response(_inbound()) as emit:
            await emit("partial")
            raise RuntimeError("turn failed")

    # The partial deltas went out, but the authoritative `message` event is left to the
    # loop's error handling rather than committing a truncated response.
    assert fake_sio.emitted == [
        ("stream", {"session_id": _SESSION_ID, "delta": "partial"}, "sid-1"),
    ]


async def test_emit_raises_when_no_sockets_remain():
    channel, fake_sio = _connected_channel()
    channel._session_to_sids.clear()

    async with channel.stream_response(_inbound()) as emit:
        with pytest.raises(RuntimeError, match="no active websocket sids"):
            await emit("Hel")

    assert fake_sio.emitted == []
