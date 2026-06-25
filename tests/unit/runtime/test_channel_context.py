"""available_channels context: ephemeral-session collapse and conversation eviction.

Regression coverage for websocket session ids cluttering the base-context
`available_channels` line. A channel whose `context_destinations()` returns an
explicitly-empty tuple collapses to its send-target placeholder instead of
enumerating every known session, and `unregister_channel_conversation` drops a
destination so disconnected sessions stop being advertised.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.runtime import channels
from app.runtime.channels import (
    get_available_channels_context,
    register_channel_conversation,
    unregister_channel_conversation,
)


class _FakeChannel:
    def __init__(self, name: str, destination_kind: str) -> None:
        self.name = name
        self.destination_kind = destination_kind


@pytest.fixture
def isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """Replace the module-level channel registry and neutralize the cache sync."""
    monkeypatch.setattr(channels, "_active_channels", {}, raising=True)
    monkeypatch.setattr(channels, "_known_channel_conversations", {}, raising=True)
    monkeypatch.setattr(channels, "_schedule_channel_conversation_sync", lambda channel_name: None)

    async def _empty_cache(channel_name: str) -> set[str]:
        return set()

    monkeypatch.setattr(channels, "_get_cached_channel_conversations", _empty_cache)
    return channels


def _enable(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", ",".join(names), raising=True)


def _seed(registry, channel: _FakeChannel, conversations: set[str] | None = None) -> None:
    registry._active_channels[channel.name] = channel
    if conversations:
        registry._known_channel_conversations[channel.name] = set(conversations)


async def test_explicit_empty_context_destinations_collapses_to_placeholder(isolated_registry, monkeypatch):
    channel = _FakeChannel("websocket", "session_id")
    channel.context_destinations = lambda known: ()  # ephemeral: never enumerate live sessions
    _seed(isolated_registry, channel, {"01ARZ3NDEKTSV4RRFFQ69G5FAV", "01BX5ZZKBKACTAV9WEVGEMMVRZ"})
    _enable(monkeypatch, "websocket")

    assert await get_available_channels_context() == ["websocket:<session_id>"]


async def test_missing_context_destinations_hook_enumerates_known(isolated_registry, monkeypatch):
    channel = _FakeChannel("telegram", "chat_id")
    _seed(isolated_registry, channel, {"123", "456"})
    _enable(monkeypatch, "telegram")

    assert await get_available_channels_context() == ["telegram:123", "telegram:456"]


async def test_unregister_conversation_stops_advertising_destination(isolated_registry, monkeypatch):
    channel = _FakeChannel("telegram", "chat_id")
    _seed(isolated_registry, channel)
    _enable(monkeypatch, "telegram")

    register_channel_conversation("telegram", "123")
    register_channel_conversation("telegram", "456")
    assert await get_available_channels_context() == ["telegram:123", "telegram:456"]

    unregister_channel_conversation("telegram", "123")
    assert await get_available_channels_context() == ["telegram:456"]

    # Last destination gone: fall back to the send-target placeholder, nothing stale lingers.
    unregister_channel_conversation("telegram", "456")
    assert await get_available_channels_context() == ["telegram:<chat_id>"]
