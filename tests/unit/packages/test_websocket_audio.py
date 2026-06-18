"""WebSocket channel audio input: base64 audio payloads become inbound attachments,
with a fail-fast guard when a proxy backend (PB_GEMINI_BASE_URL) is configured."""

from __future__ import annotations

import base64
import os

import pytest

os.environ.setdefault("PB_WEBSOCKET_BEARER_TOKEN", "test-token")
websocket_channel = pytest.importorskip("pillbug_websocket.websocket_channel")

WebSocketChannel = websocket_channel.WebSocketChannel

_SESSION_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


class _FakeSio:
    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict, str | None]] = []

    async def emit(self, event: str, payload: dict, to: str | None = None) -> None:
        self.emitted.append((event, payload, to))


def _connected_channel() -> tuple[WebSocketChannel, _FakeSio]:
    channel = WebSocketChannel()
    fake_sio = _FakeSio()
    channel._sio = fake_sio
    channel._session_to_sids[_SESSION_ID] = {"sid-1"}
    channel._sid_to_session["sid-1"] = _SESSION_ID
    return channel, fake_sio


def _audio_message(
    raw: bytes = b"\x00\x01\x02",
    *,
    mime_type: str = "audio/wav",
    filename: str | None = "clip.wav",
    text: str | None = None,
) -> dict:
    audio: dict = {"data": base64.b64encode(raw).decode(), "mime_type": mime_type}
    if filename is not None:
        audio["filename"] = filename
    payload: dict = {"audio": audio}
    if text is not None:
        payload["text"] = text
    return payload


@pytest.fixture
def real_backend(tmp_path, monkeypatch):
    """Real Gemini backend (no proxy) writing into an isolated workspace root."""
    monkeypatch.setattr(websocket_channel.core_settings, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(websocket_channel.core_settings, "GEMINI_BASE_URL", None)
    return tmp_path


async def test_audio_message_writes_attachment_and_enqueues(real_backend):
    channel, fake_sio = _connected_channel()

    await channel._on_message("sid-1", _audio_message(b"\x00\x01\x02", text="please transcribe"))

    message = channel._inbound_queue.get_nowait()
    assert message.channel_name == "websocket"
    assert message.conversation_id == _SESSION_ID
    assert "please transcribe" in message.text

    attachments = message.metadata["inbound_attachments"]
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment["source"] == "websocket"
    assert attachment["kind"] == "audio"
    assert attachment["mime_type"] == "audio/wav"
    assert attachment["path"].startswith("inbox/websocket/")

    stored = real_backend / attachment["path"]
    assert stored.is_file()
    assert stored.read_bytes() == b"\x00\x01\x02"

    assert all(event != "error" for event, _, _ in fake_sio.emitted)


async def test_audio_rejected_when_proxy_backend_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(websocket_channel.core_settings, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(websocket_channel.core_settings, "GEMINI_BASE_URL", "http://proxy:8080")
    channel, fake_sio = _connected_channel()

    await channel._on_message("sid-1", _audio_message())

    assert channel._inbound_queue.empty()
    assert not any(tmp_path.rglob("*"))  # nothing written to the workspace
    event, payload, to = fake_sio.emitted[-1]
    assert event == "error"
    assert to == "sid-1"
    assert "Gemini backend" in payload["error"]


async def test_audio_rejected_when_oversized(real_backend, monkeypatch):
    monkeypatch.setattr(websocket_channel.settings, "MAX_AUDIO_BYTES", 4)
    channel, fake_sio = _connected_channel()

    await channel._on_message("sid-1", _audio_message(b"\x00\x01\x02\x03\x04\x05"))

    assert channel._inbound_queue.empty()
    event, payload, _ = fake_sio.emitted[-1]
    assert event == "error"
    assert "limit" in payload["error"]


async def test_non_audio_mime_rejected(real_backend):
    channel, fake_sio = _connected_channel()

    await channel._on_message("sid-1", _audio_message(mime_type="application/pdf"))

    assert channel._inbound_queue.empty()
    assert fake_sio.emitted[-1][0] == "error"


async def test_invalid_base64_rejected(real_backend):
    channel, fake_sio = _connected_channel()

    await channel._on_message("sid-1", {"audio": {"data": "!!!not-base64!!!", "mime_type": "audio/wav"}})

    assert channel._inbound_queue.empty()
    assert fake_sio.emitted[-1][0] == "error"


async def test_text_message_still_enqueues_unchanged():
    channel, fake_sio = _connected_channel()

    await channel._on_message("sid-1", {"text": "hello"})

    message = channel._inbound_queue.get_nowait()
    assert message.text == "hello"
    assert "inbound_attachments" not in message.metadata
    assert fake_sio.emitted == []
