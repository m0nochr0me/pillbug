"""ApplicationLoop streaming wiring: PB_STREAMING_CHANNELS gating and delivery fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.core import ai as ai_mod
from app.core.ai import ChatResponse
from app.core.config import settings
from app.runtime.channels import BaseChannel
from app.runtime.loop import ApplicationLoop
from app.runtime.pipeline import InboundProcessingPipeline
from app.schema.messages import InboundBatch, InboundMessage, OutboundAttachment


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli")
    monkeypatch.setattr(settings, "CHANNEL_PLUGIN_FACTORIES", "")
    (settings.WORKSPACE_ROOT / "AGENTS.md").write_text("# AGENTS.md\nstable\n", encoding="utf-8")
    return settings


@pytest.fixture
def application_loop(workspace_settings):
    service = ai_mod.GeminiChatService()
    return ApplicationLoop(chat_service=service, channels=[], pipeline=InboundProcessingPipeline())


class _StreamingChannel(BaseChannel):
    name = "fakechan"
    destination_kind = "implicit"

    def __init__(self, *, emit_error: Exception | None = None) -> None:
        self._emit_error = emit_error
        self.streamed: list[str] = []
        self.stream_completed = False
        self.sent: list[str] = []

    async def listen(self) -> AsyncIterator[InboundMessage]:
        return
        yield

    async def send_message(
        self,
        conversation_id: str,
        message_text: str,
        metadata: dict[str, object] | None = None,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        self.sent.append(message_text)

    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        self.sent.append(response_text)

    @asynccontextmanager
    async def stream_response(
        self,
        inbound_message: InboundMessage,
    ) -> AsyncIterator[Callable[[str], Awaitable[None]]]:
        async def emit(delta: str) -> None:
            if self._emit_error is not None:
                raise self._emit_error
            self.streamed.append(delta)

        yield emit
        self.stream_completed = True


class _FakeSession:
    def __init__(self, response_text: str = "Hello world") -> None:
        self._response_text = response_text

    async def send_message(
        self,
        message,
        message_metadata=None,
        channel_name=None,
        max_remote_calls=None,
        on_text_delta=None,
    ) -> ChatResponse:
        if on_text_delta is not None:
            await on_text_delta("Hello ")
            await on_text_delta("world")
        return ChatResponse(text=self._response_text)

    def total_token_count(self) -> int:
        return 0


def _batch() -> InboundBatch:
    return InboundBatch(
        messages=(
            InboundMessage(
                channel_name="fakechan",
                conversation_id="c1",
                user_id="u1",
                text="hi",
            ),
        )
    )


async def _run_turn(application_loop: ApplicationLoop, channel: _StreamingChannel) -> None:
    await application_loop._send_session_response(
        channel=channel,
        batch=_batch(),
        session=_FakeSession(),
        model_input="hi",
    )


async def test_streaming_channel_receives_deltas_without_full_send(application_loop, monkeypatch):
    monkeypatch.setattr(settings, "STREAMING_CHANNELS", "fakechan")
    channel = _StreamingChannel()

    await _run_turn(application_loop, channel)

    assert channel.streamed == ["Hello ", "world"]
    assert channel.stream_completed
    # The stream context owns delivery; no duplicate full-response send follows.
    assert channel.sent == []


async def test_channel_not_in_streaming_channels_gets_full_send(application_loop, monkeypatch):
    monkeypatch.setattr(settings, "STREAMING_CHANNELS", "websocket")
    channel = _StreamingChannel()

    await _run_turn(application_loop, channel)

    assert channel.streamed == []
    assert channel.sent == ["Hello world"]


async def test_emit_failure_falls_back_to_full_send(application_loop, monkeypatch):
    monkeypatch.setattr(settings, "STREAMING_CHANNELS", "fakechan")
    channel = _StreamingChannel(emit_error=RuntimeError("socket gone"))

    await _run_turn(application_loop, channel)

    assert channel.streamed == []
    assert channel.sent == ["Hello world"]
