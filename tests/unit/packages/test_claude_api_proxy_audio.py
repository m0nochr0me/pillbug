"""Tests for pillbug_claude_api_proxy.audio inbound-audio pre-pass."""

from __future__ import annotations

import base64

import httpx
import pytest

audio = pytest.importorskip("pillbug_claude_api_proxy.audio")


@pytest.fixture(autouse=True)
def _reset_audio_state(monkeypatch):
    audio._TRANSCRIPT_CACHE.clear()
    monkeypatch.setattr(audio, "_warned_missing_key", False)
    yield
    audio._TRANSCRIPT_CACHE.clear()


def _audio_part(raw: bytes, *, mime: str = "audio/ogg", key: str = "inlineData") -> dict:
    # google-genai serializes inline bytes with URL-safe base64.
    data = base64.urlsafe_b64encode(raw).decode("ascii")
    return {key: {"mimeType": mime, "data": data}}


def _make_fake_client(calls: list, *, json_data: dict | None = None, status_error: bool = False, post_exc=None):
    class _Resp:
        def raise_for_status(self):
            if status_error:
                raise httpx.HTTPError("upstream error")

        def json(self):
            return json_data if json_data is not None else {}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, headers, data, files):
            calls.append({"url": url, "headers": headers, "data": data, "files": files})
            if post_exc is not None:
                raise post_exc
            return _Resp()

    return _Client


async def test_placeholder_mode_rewrites_audio_and_leaves_other_parts(monkeypatch):
    monkeypatch.setattr(audio.settings, "AUDIO_MODE", "placeholder")
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "listen to this"},
                    _audio_part(b"\x00\x01\x02"),
                    {"functionCall": {"name": "tool", "args": {}}},
                ],
            }
        ]
    }

    await audio.transcribe_inbound_audio(payload)

    parts = payload["contents"][0]["parts"]
    assert parts[0] == {"text": "listen to this"}
    assert "inlineData" not in parts[1]
    assert "Voice/audio message (audio/ogg)" in parts[1]["text"]
    assert parts[2] == {"functionCall": {"name": "tool", "args": {}}}


async def test_elevenlabs_mode_transcribes_inline_audio(monkeypatch):
    calls: list = []
    monkeypatch.setattr(audio.settings, "AUDIO_MODE", "elevenlabs")
    monkeypatch.setattr(audio.settings, "ELEVENLABS_MODEL", "scribe_v2")
    monkeypatch.setattr(audio.settings, "ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
    monkeypatch.setattr(type(audio.settings), "resolved_elevenlabs_api_key", lambda self: "k-123")
    monkeypatch.setattr(audio.httpx, "AsyncClient", _make_fake_client(calls, json_data={"text": "hello there"}))

    # b"\xff\xff\xff" encodes to URL-safe "____"; a standard-alphabet decode would
    # reject it, so a correct round-trip proves the URL-safe path is used.
    raw = b"\xff\xff\xff"
    payload = {"contents": [{"role": "user", "parts": [_audio_part(raw, mime="audio/ogg")]}]}

    await audio.transcribe_inbound_audio(payload)

    assert payload["contents"][0]["parts"][0] == {"text": "[Voice message transcript: hello there]"}
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://api.elevenlabs.io/v1/speech-to-text"
    assert call["headers"]["xi-api-key"] == "k-123"
    assert call["data"]["model_id"] == "scribe_v2"
    filename, content, content_type = call["files"]["file"]
    assert content == raw
    assert content_type == "audio/ogg"
    assert filename.startswith("audio")


async def test_elevenlabs_failure_falls_back_to_placeholder(monkeypatch):
    calls: list = []
    monkeypatch.setattr(audio.settings, "AUDIO_MODE", "elevenlabs")
    monkeypatch.setattr(type(audio.settings), "resolved_elevenlabs_api_key", lambda self: "k")
    monkeypatch.setattr(audio.httpx, "AsyncClient", _make_fake_client(calls, status_error=True))
    payload = {"contents": [{"role": "user", "parts": [_audio_part(b"abc")]}]}

    await audio.transcribe_inbound_audio(payload)

    assert "could not be transcribed" in payload["contents"][0]["parts"][0]["text"]
    assert len(calls) == 1


async def test_identical_audio_is_transcribed_once(monkeypatch):
    calls: list = []
    monkeypatch.setattr(audio.settings, "AUDIO_MODE", "elevenlabs")
    monkeypatch.setattr(type(audio.settings), "resolved_elevenlabs_api_key", lambda self: "k")
    monkeypatch.setattr(audio.httpx, "AsyncClient", _make_fake_client(calls, json_data={"text": "once"}))
    raw = b"same-clip-bytes"
    payload = {
        "contents": [
            {"role": "user", "parts": [_audio_part(raw)]},
            {"role": "model", "parts": [{"text": "ok"}]},
            {"role": "user", "parts": [_audio_part(raw), {"text": "again"}]},
        ]
    }

    await audio.transcribe_inbound_audio(payload)

    # The second identical clip is served from the content-hash cache.
    assert len(calls) == 1
    assert payload["contents"][0]["parts"][0] == {"text": "[Voice message transcript: once]"}
    assert payload["contents"][2]["parts"][0] == {"text": "[Voice message transcript: once]"}


async def test_file_data_audio_becomes_placeholder_even_in_elevenlabs_mode(monkeypatch):
    monkeypatch.setattr(audio.settings, "AUDIO_MODE", "elevenlabs")
    monkeypatch.setattr(type(audio.settings), "resolved_elevenlabs_api_key", lambda self: "k")
    payload = {
        "contents": [{"role": "user", "parts": [{"fileData": {"mimeType": "audio/ogg", "fileUri": "https://files/x"}}]}]
    }

    await audio.transcribe_inbound_audio(payload)

    assert "too large" in payload["contents"][0]["parts"][0]["text"]


async def test_elevenlabs_mode_without_key_uses_placeholder(monkeypatch):
    monkeypatch.setattr(audio.settings, "AUDIO_MODE", "elevenlabs")
    monkeypatch.setattr(type(audio.settings), "resolved_elevenlabs_api_key", lambda self: None)
    payload = {"contents": [{"role": "user", "parts": [_audio_part(b"x")]}]}

    await audio.transcribe_inbound_audio(payload)

    assert "was not transcribed" in payload["contents"][0]["parts"][0]["text"]


async def test_non_audio_inline_data_is_untouched(monkeypatch):
    monkeypatch.setattr(audio.settings, "AUDIO_MODE", "placeholder")
    image_part = {"inlineData": {"mimeType": "image/png", "data": "Zm9v"}}
    payload = {"contents": [{"role": "user", "parts": [image_part]}]}

    await audio.transcribe_inbound_audio(payload)

    assert payload["contents"][0]["parts"][0] == image_part


def test_invalid_audio_mode_is_rejected():
    from pillbug_claude_api_proxy.config import ProxySettings
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ProxySettings(_env_file=None, AUDIO_MODE="bogus")
