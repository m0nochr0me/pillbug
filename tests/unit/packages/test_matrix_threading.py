"""Threading behavior for the Matrix channel: conversation_id encoding and m.relates_to shape."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

matrix_channel = pytest.importorskip("pillbug_matrix.matrix_channel")

_parse_conversation_id = matrix_channel._parse_conversation_id
_build_conversation_id = matrix_channel._build_conversation_id
_extract_thread_root = matrix_channel._extract_thread_root


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
