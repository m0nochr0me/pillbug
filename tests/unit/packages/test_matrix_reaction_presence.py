"""Reaction-based presence indicator for the Matrix channel."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schema.messages import InboundMessage

matrix_channel = pytest.importorskip("pillbug_matrix.matrix_channel")

MatrixChannel = matrix_channel.MatrixChannel
MatrixChannelSettings = matrix_channel.MatrixChannelSettings


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict]] = []
        self.redacted: list[tuple[str, str]] = []
        self.typing: list[tuple[str, bool]] = []

    async def room_send(self, *, room_id: str, message_type: str, content: dict) -> object:
        self.sent.append((room_id, message_type, content))
        return SimpleNamespace(event_id="$reaction_evt")

    async def room_redact(self, room_id: str, event_id: str) -> object:
        self.redacted.append((room_id, event_id))
        return SimpleNamespace()

    async def room_typing(self, room_id: str, typing_state: bool = True, timeout: int = 0) -> object:
        self.typing.append((room_id, typing_state))
        return SimpleNamespace()

    async def close(self) -> None:
        return None


def _channel(reaction_presence: bool) -> MatrixChannel:
    settings = MatrixChannelSettings(
        homeserver_url="https://hs.example",
        access_token="tok",
        user_id="@bot:hs.example",
        reaction_presence=reaction_presence,
    )
    channel = MatrixChannel(settings)
    channel._client = _FakeClient()  # type: ignore[assignment]
    return channel


def _inbound(metadata: dict[str, object]) -> InboundMessage:
    return InboundMessage(
        channel_name="matrix",
        conversation_id="!room:hs.example",
        user_id="@user:hs.example",
        text="hello",
        metadata=metadata,
    )


def test_build_reaction_content_uses_annotation_relation() -> None:
    assert MatrixChannel._build_reaction_content("$user_evt", "🤔") == {
        "m.relates_to": {
            "rel_type": "m.annotation",
            "event_id": "$user_evt",
            "key": "🤔",
        }
    }


async def test_reaction_presence_adds_then_redacts() -> None:
    channel = _channel(reaction_presence=True)
    fake: _FakeClient = channel._client  # type: ignore[assignment]
    inbound = _inbound({"matrix_room_id": "!room:hs.example", "matrix_event_id": "$user_evt"})

    async with channel.response_presence(inbound):
        assert fake.sent == [
            (
                "!room:hs.example",
                "m.reaction",
                {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$user_evt", "key": "🤔"}},
            )
        ]
        assert fake.redacted == []

    assert fake.redacted == [("!room:hs.example", "$reaction_evt")]


async def test_reaction_presence_redacts_even_when_response_raises() -> None:
    channel = _channel(reaction_presence=True)
    fake: _FakeClient = channel._client  # type: ignore[assignment]
    inbound = _inbound({"matrix_room_id": "!room:hs.example", "matrix_event_id": "$user_evt"})

    with pytest.raises(RuntimeError):
        async with channel.response_presence(inbound):
            raise RuntimeError("model failed")

    assert fake.redacted == [("!room:hs.example", "$reaction_evt")]


async def test_reaction_presence_skips_when_no_target_event_id() -> None:
    channel = _channel(reaction_presence=True)
    fake: _FakeClient = channel._client  # type: ignore[assignment]
    inbound = _inbound({"matrix_room_id": "!room:hs.example"})

    async with channel.response_presence(inbound):
        pass

    assert fake.sent == []
    assert fake.redacted == []


async def test_typing_presence_used_when_reaction_disabled() -> None:
    channel = _channel(reaction_presence=False)
    fake: _FakeClient = channel._client  # type: ignore[assignment]
    inbound = _inbound({"matrix_room_id": "!room:hs.example", "matrix_event_id": "$user_evt"})

    async with channel.response_presence(inbound):
        pass

    assert fake.sent == []
    assert fake.redacted == []
