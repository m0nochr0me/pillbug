"""Threading behavior for the Matrix channel: conversation_id encoding and m.relates_to shape."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.schema.messages import InboundMessage, OutboundAttachment

matrix_channel = pytest.importorskip("pillbug_matrix.matrix_channel")

MatrixChannel = matrix_channel.MatrixChannel
MatrixChannelSettings = matrix_channel.MatrixChannelSettings

_parse_conversation_id = matrix_channel._parse_conversation_id
_build_conversation_id = matrix_channel._build_conversation_id
_extract_thread_root = matrix_channel._extract_thread_root


class _RecordingClient:
    """Records room_send calls and hands back distinct event ids per send."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.uploads: list[str] = []
        self._counter = 0

    async def room_send(self, *, room_id: str, message_type: str, content: dict) -> object:
        self._counter += 1
        self.sent.append({"room_id": room_id, "message_type": message_type, "content": content})
        return SimpleNamespace(event_id=f"$evt{self._counter}")

    async def upload(self, *, data_provider: object, content_type: str, filename: str, filesize: int) -> object:
        del data_provider, content_type, filesize
        self.uploads.append(filename)
        return SimpleNamespace(content_uri=f"mxc://hs.example/{filename}"), None

    async def close(self) -> None:
        return None


def _channel(*, reply_in_thread: bool) -> MatrixChannel:
    settings = MatrixChannelSettings(
        homeserver_url="https://hs.example",
        access_token="tok",
        user_id="@bot:hs.example",
        reply_in_thread=reply_in_thread,
    )
    channel = MatrixChannel(settings)
    channel._client = _RecordingClient()  # type: ignore[assignment]
    return channel


@dataclass
class _FakeEvent:
    event_id: str
    source: dict = field(default_factory=dict)


class _FakeSettings:
    def __init__(self, *, reply_to_message: bool, reply_in_thread: bool) -> None:
        self.reply_to_message = reply_to_message
        self.reply_in_thread = reply_in_thread


class _ChannelStub:
    """Minimal channel stub exposing only the relates_to builder under test."""

    def __init__(self, *, reply_to_message: bool, reply_in_thread: bool) -> None:
        self._settings = _FakeSettings(
            reply_to_message=reply_to_message,
            reply_in_thread=reply_in_thread,
        )

    _build_relates_to = matrix_channel.MatrixChannel._build_relates_to


def test_parse_conversation_id_preserves_colons_in_room_id() -> None:
    """Room IDs like `!room:server.example` must not be split on `:`."""
    room_id, thread_root = _parse_conversation_id("!room:server.example")
    assert room_id == "!room:server.example"
    assert thread_root is None


def test_parse_conversation_id_with_thread_root() -> None:
    room_id, thread_root = _parse_conversation_id("!room:server.example|$evt:server.example")
    assert room_id == "!room:server.example"
    assert thread_root == "$evt:server.example"


def test_parse_conversation_id_empty_room_rejected() -> None:
    with pytest.raises(ValueError):
        _parse_conversation_id("")


def test_build_conversation_id_omits_empty_thread() -> None:
    assert _build_conversation_id("!room:server", None) == "!room:server"
    assert _build_conversation_id("!room:server", "$evt") == "!room:server|$evt"


def test_extract_thread_root_returns_event_id_for_thread_relation() -> None:
    event = _FakeEvent(
        event_id="$child",
        source={
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "$root",
                    "is_falling_back": True,
                }
            }
        },
    )
    assert _extract_thread_root(event) == "$root"


def test_extract_thread_root_ignores_non_thread_relations() -> None:
    event = _FakeEvent(
        event_id="$child",
        source={"content": {"m.relates_to": {"m.in_reply_to": {"event_id": "$other"}}}},
    )
    assert _extract_thread_root(event) is None


def test_extract_thread_root_handles_missing_relates_to() -> None:
    event = _FakeEvent(event_id="$child", source={"content": {}})
    assert _extract_thread_root(event) is None


def test_build_relates_to_thread_includes_falling_back_in_reply_to() -> None:
    channel = _ChannelStub(reply_to_message=True, reply_in_thread=True)
    relates_to = channel._build_relates_to(
        thread_root_event_id="$root",
        reply_to_event_id="$latest_in_thread",
    )
    assert relates_to == {
        "rel_type": "m.thread",
        "event_id": "$root",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$latest_in_thread"},
    }


def test_build_relates_to_thread_falls_back_to_root_when_no_inbound() -> None:
    channel = _ChannelStub(reply_to_message=False, reply_in_thread=True)
    relates_to = channel._build_relates_to(thread_root_event_id="$root", reply_to_event_id=None)
    assert relates_to == {
        "rel_type": "m.thread",
        "event_id": "$root",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$root"},
    }


def test_build_relates_to_non_thread_respects_reply_to_message_flag() -> None:
    channel_with_reply = _ChannelStub(reply_to_message=True, reply_in_thread=False)
    assert channel_with_reply._build_relates_to(
        thread_root_event_id=None,
        reply_to_event_id="$evt",
    ) == {"m.in_reply_to": {"event_id": "$evt"}}

    channel_without_reply = _ChannelStub(reply_to_message=False, reply_in_thread=False)
    assert (
        channel_without_reply._build_relates_to(
            thread_root_event_id=None,
            reply_to_event_id="$evt",
        )
        is None
    )


def test_build_relates_to_returns_none_when_nothing_relevant() -> None:
    channel = _ChannelStub(reply_to_message=True, reply_in_thread=False)
    assert channel._build_relates_to(thread_root_event_id=None, reply_to_event_id=None) is None


async def test_send_message_starts_new_thread_when_threading_enabled() -> None:
    """A proactive send must not be appended to a thread root carried in the conversation_id."""
    channel = _channel(reply_in_thread=True)
    client: _RecordingClient = channel._client  # type: ignore[assignment]

    await channel.send_message("!room:hs.example|$stale_root", "ping")

    assert len(client.sent) == 1
    assert client.sent[0]["room_id"] == "!room:hs.example"
    # A new top-level message roots a fresh thread; the stale root is never referenced.
    assert "m.relates_to" not in client.sent[0]["content"]


async def test_send_message_threads_chunks_under_new_root() -> None:
    """A chunked proactive message stays in one new thread rooted at its first chunk."""
    channel = _channel(reply_in_thread=True)
    client: _RecordingClient = channel._client  # type: ignore[assignment]

    long_text = ("a" * 3000) + "\n\n" + ("b" * 3000)
    await channel.send_message("!room:hs.example|$stale_root", long_text)

    assert len(client.sent) == 2
    first, second = client.sent
    # First chunk roots the new thread (top-level, no relation).
    assert "m.relates_to" not in first["content"]
    # Later chunks thread under the NEW root ($evt1), never the stale root.
    assert second["content"]["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$evt1",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$evt1"},
    }


async def test_send_message_preserves_explicit_thread_when_threading_disabled() -> None:
    """With threading off, a thread root explicitly targeted in the conversation_id is honored."""
    channel = _channel(reply_in_thread=False)
    client: _RecordingClient = channel._client  # type: ignore[assignment]

    await channel.send_message("!room:hs.example|$explicit_root", "ping")

    assert len(client.sent) == 1
    assert client.sent[0]["content"]["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$explicit_root",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$explicit_root"},
    }


async def test_send_response_still_threads_into_inbound_thread() -> None:
    """Regression guard: the direct reply path keeps replying inside the inbound thread."""
    channel = _channel(reply_in_thread=True)
    client: _RecordingClient = channel._client  # type: ignore[assignment]

    inbound = InboundMessage(
        channel_name="matrix",
        conversation_id="!room:hs.example|$root",
        user_id="@user:hs.example",
        text="hello",
        metadata={
            "matrix_room_id": "!room:hs.example",
            "matrix_event_id": "$user_evt",
            "matrix_thread_root_event_id": "$root",
        },
    )
    await channel.send_response(inbound, "pong")

    assert len(client.sent) == 1
    assert client.sent[0]["content"]["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$root",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$user_evt"},
    }


async def test_send_message_attachments_only_root_new_thread(tmp_path) -> None:
    """Attachment-only proactive sends start their own thread instead of reusing the stale root."""
    channel = _channel(reply_in_thread=True)
    client: _RecordingClient = channel._client  # type: ignore[assignment]

    file_a = tmp_path / "a.txt"
    file_a.write_text("alpha")
    file_b = tmp_path / "b.txt"
    file_b.write_text("beta")

    await channel.send_message(
        "!room:hs.example|$stale_root",
        "",
        attachments=(OutboundAttachment(path=str(file_a)), OutboundAttachment(path=str(file_b))),
    )

    assert len(client.sent) == 2
    first, second = client.sent
    # First attachment roots the new thread; the rest thread under it, never the stale root.
    assert "m.relates_to" not in first["content"]
    assert second["content"]["m.relates_to"]["rel_type"] == "m.thread"
    assert second["content"]["m.relates_to"]["event_id"] == "$evt1"


async def test_send_message_attachment_joins_text_new_thread(tmp_path) -> None:
    """A text + file proactive send keeps the file in the same new thread as the text root."""
    channel = _channel(reply_in_thread=True)
    client: _RecordingClient = channel._client  # type: ignore[assignment]

    report = tmp_path / "report.txt"
    report.write_text("done")

    await channel.send_message(
        "!room:hs.example|$stale_root",
        "report ready",
        attachments=(OutboundAttachment(path=str(report)),),
    )

    assert len(client.sent) == 2
    text_send, attachment_send = client.sent
    # Text roots the new thread ($evt1); the attachment threads under it.
    assert "m.relates_to" not in text_send["content"]
    assert attachment_send["content"]["m.relates_to"]["event_id"] == "$evt1"
