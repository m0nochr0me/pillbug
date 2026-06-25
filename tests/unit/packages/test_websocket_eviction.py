"""WebSocket session eviction: a client disconnect and an idle expiry both drop the
session id from the shared channel-conversation registry, so stale sessions stop
cluttering the base-context `available_channels` line."""

from __future__ import annotations

import os
import time

import pytest

from app.runtime import channels

os.environ.setdefault("PB_WEBSOCKET_BEARER_TOKEN", "test-token")
websocket_channel = pytest.importorskip("pillbug_websocket.websocket_channel")

WebSocketChannel = websocket_channel.WebSocketChannel

_SESSION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


class _FakeSio:
    def __init__(self) -> None:
        self.disconnected: list[str] = []

    async def disconnect(self, sid: str) -> None:
        self.disconnected.append(sid)


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """Isolate the shared registry and keep eviction off the cache/event loop."""
    monkeypatch.setattr(channels, "_known_channel_conversations", {}, raising=True)
    monkeypatch.setattr(channels, "_schedule_channel_conversation_sync", lambda channel_name: None)
    return channels


def _connected_channel() -> tuple[WebSocketChannel, _FakeSio]:
    channel = WebSocketChannel()
    fake_sio = _FakeSio()
    channel._sio = fake_sio
    channel._sid_to_session["sid-1"] = _SESSION_ID
    channel._session_to_sids[_SESSION_ID] = {"sid-1"}
    channel._session_last_activity[_SESSION_ID] = time.monotonic()
    return channel, fake_sio


async def test_disconnect_evicts_conversation(isolated_registry):
    channel, _ = _connected_channel()
    channels.register_channel_conversation("websocket", _SESSION_ID)
    assert _SESSION_ID in channels._known_channel_conversations["websocket"]

    await channel._on_disconnect("sid-1")

    assert _SESSION_ID not in channels._known_channel_conversations.get("websocket", set())


async def test_idle_expiry_evicts_conversation(isolated_registry, monkeypatch):
    channel, fake_sio = _connected_channel()
    channels.register_channel_conversation("websocket", _SESSION_ID)
    monkeypatch.setattr(websocket_channel.settings, "IDLE_TIMEOUT_SECONDS", 0, raising=True)
    channel._session_last_activity[_SESSION_ID] = time.monotonic() - 1.0

    await channel._evict_idle_sessions()

    assert fake_sio.disconnected == ["sid-1"]
    assert _SESSION_ID not in channels._known_channel_conversations.get("websocket", set())
