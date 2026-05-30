"""Inbound-audio handling for the Gemini→Anthropic-API proxy.

Claude has no native audio input modality (the Anthropic Messages API
`ContentBlockParam` union has no audio block), so voice notes that Pillbug
forwards as Gemini `inline_data` audio parts cannot reach the model as audio.
This module runs as an async pre-pass over the request payload BEFORE
`translate.extract_history` and rewrites every audio part into a text part, so
audio never reaches the image-or-drop path in `translate.py`.

Two modes (``PB_CLAUDE_API_PROXY_AUDIO_MODE``):

- ``placeholder``: replace audio with a short, model-actionable note.
- ``elevenlabs``: transcribe inline audio via ElevenLabs Scribe and replace it
  with the transcript; fall back to the placeholder on any failure or when no
  API key is resolvable.

Only ``inline_data`` (``Part.from_bytes``, ≤8 MiB in Pillbug) can be
transcribed; larger attachments arrive as a Gemini file URI (``file_data``) the
proxy cannot fetch, so those always become a placeholder. Transcripts are cached
by audio content hash so the same clip re-sent across turns is billed once.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import mimetypes
from typing import Any

import httpx
from loguru import logger

from pillbug_claude_api_proxy.config import settings

__all__ = ("transcribe_inbound_audio",)

_ELEVENLABS_TIMEOUT_SECONDS = 120.0
_TRANSCRIPT_CACHE: dict[str, str] = {}
_TRANSCRIPT_CACHE_MAX = 256

_warned_missing_key = False


def _pick(part: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in part:
            return part[name]
    return None


def _audio_mime(media: Any) -> str | None:
    if not isinstance(media, dict):
        return None
    mime_type = media.get("mimeType") or media.get("mime_type")
    if isinstance(mime_type, str) and mime_type.lower().startswith("audio/"):
        return mime_type
    return None


def _placeholder(mime_type: str, *, reason: str = "was not transcribed") -> dict[str, str]:
    return {
        "text": (
            f"[Voice/audio message ({mime_type}) {reason}. "
            "Ask the user to send the content as text if your reply depends on it.]"
        )
    }


def _decode_inline_audio(data: Any) -> bytes | None:
    if not isinstance(data, str) or not data:
        return None
    # google-genai serializes inline bytes with URL-safe base64 ('-'/'_'); tolerate
    # the standard alphabet too in case a caller already normalized it.
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return decoder(data)
        except binascii.Error, ValueError:
            continue
    return None


async def _scribe_transcribe(audio_bytes: bytes, mime_type: str, api_key: str) -> str | None:
    digest = hashlib.sha256(audio_bytes).hexdigest()
    cached = _TRANSCRIPT_CACHE.get(digest)
    if cached is not None:
        return cached

    extension = mimetypes.guess_extension(mime_type) or ".bin"
    url = settings.ELEVENLABS_BASE_URL.rstrip("/") + "/v1/speech-to-text"
    try:
        async with httpx.AsyncClient(timeout=_ELEVENLABS_TIMEOUT_SECONDS) as client:
            response = await client.post(
                url,
                headers={"xi-api-key": api_key},
                data={"model_id": settings.ELEVENLABS_MODEL},
                files={"file": (f"audio{extension}", audio_bytes, mime_type)},
            )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(f"ElevenLabs transcription failed (mime={mime_type}): {exc}")
        return None

    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip():
        logger.warning(f"ElevenLabs returned no transcript text (mime={mime_type})")
        return None

    transcript = text.strip()
    if len(_TRANSCRIPT_CACHE) >= _TRANSCRIPT_CACHE_MAX:
        _TRANSCRIPT_CACHE.pop(next(iter(_TRANSCRIPT_CACHE)))
    _TRANSCRIPT_CACHE[digest] = transcript
    return transcript


async def _rewrite_audio_part(part: dict[str, Any], *, mode: str, api_key: str | None) -> dict[str, Any] | None:
    """Return a replacement text part for an audio part, or None to leave it unchanged."""

    inline = _pick(part, "inlineData", "inline_data")
    inline_mime = _audio_mime(inline)
    if inline_mime is not None:
        if mode == "elevenlabs" and api_key:
            audio_bytes = _decode_inline_audio(inline.get("data"))
            if audio_bytes is None:
                return _placeholder(inline_mime, reason="could not be decoded")
            transcript = await _scribe_transcribe(audio_bytes, inline_mime, api_key)
            if transcript is not None:
                return {"text": f"[Voice message transcript: {transcript}]"}
            return _placeholder(inline_mime, reason="could not be transcribed")
        return _placeholder(inline_mime)

    file_mime = _audio_mime(_pick(part, "fileData", "file_data"))
    if file_mime is not None:
        # >8 MiB audio arrives as a Gemini file URI the proxy cannot fetch.
        return _placeholder(file_mime, reason="was too large to transcribe")

    return None


async def transcribe_inbound_audio(payload: dict[str, Any]) -> None:
    """Rewrite audio parts in ``payload['contents']`` in place (see module docstring)."""

    global _warned_missing_key

    contents = payload.get("contents")
    if isinstance(contents, dict):
        contents = [contents]
    if not isinstance(contents, list):
        return

    mode = settings.AUDIO_MODE
    api_key = await asyncio.to_thread(settings.resolved_elevenlabs_api_key) if mode == "elevenlabs" else None
    if mode == "elevenlabs" and not api_key and not _warned_missing_key:
        logger.warning(
            "PB_CLAUDE_API_PROXY_AUDIO_MODE=elevenlabs but no ELEVENLABS_API_KEY resolved "
            "(/run/secrets/elevenlabs_api_key or env); inbound audio will use the placeholder instead."
        )
        _warned_missing_key = True

    for entry in contents:
        if not isinstance(entry, dict):
            continue
        parts = entry.get("parts")
        if not isinstance(parts, list):
            continue
        for index, part in enumerate(parts):
            if not isinstance(part, dict):
                continue
            replacement = await _rewrite_audio_part(part, mode=mode, api_key=api_key)
            if replacement is not None:
                parts[index] = replacement
