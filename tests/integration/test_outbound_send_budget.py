"""send_message respects per-channel rolling-window budget (plan P2 #15)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from app import mcp as mcp_mod
from app.core.config import settings
from app.runtime import channels as channels_mod
from app.runtime.approvals import outbound_draft_store
from app.runtime.outbound_budget import outbound_send_budget
from app.schema.messages import InboundMessage, OutboundAttachment


class _RecordingChannel:
    destination_kind = "explicit"

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.sent: list[dict[str, Any]] = []

    async def listen(self) -> AsyncIterator[InboundMessage]:
        if False:
            yield  # pragma: no cover

    async def send_message(
        self,
        conversation_id: str,
        message_text: str,
        metadata: dict[str, object] | None = None,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        self.sent.append({"conversation_id": conversation_id, "message": message_text})

    async def send_response(self, inbound_message, response_text, attachments=None) -> None:  # pragma: no cover
        return None

    @asynccontextmanager
    async def response_presence(self, inbound_message):  # pragma: no cover
        yield

    async def close(self) -> None:  # pragma: no cover
        return None


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    outbound_draft_store._cache.clear()
    outbound_draft_store._loaded_dir = None
    outbound_send_budget.reset()
    return settings


@pytest.fixture
def fake_channel(workspace_settings, monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli,fake")
    monkeypatch.setattr(settings, "OUTBOUND_AUTOSEND_CHANNELS", "cli,fake")
    channel = _RecordingChannel("fake")
    channels_mod._active_channels["fake"] = channel
    try:
        yield channel
    finally:
        channels_mod._active_channels.pop("fake", None)


async def test_send_message_rate_limited_when_per_minute_exceeded(monkeypatch, fake_channel):
    monkeypatch.setattr(settings, "OUTBOUND_SEND_LIMITS_JSON", '{"fake": {"per_minute": 2}}')

    for _ in range(2):
        result = await mcp_mod.send_message("fake:abc", "hi")
        assert result.get("status") != "error", result

    blocked = await mcp_mod.send_message("fake:abc", "spam")
    assert blocked["status"] == "error"
    assert blocked["type"] == "rate_limited"
    assert blocked["details"]["reason"] == "per_minute_exceeded"
    assert blocked["details"]["channel"] == "fake"
    assert "wait" in blocked["next_valid_actions"]
    assert len(fake_channel.sent) == 2  # 3rd never dispatched


async def test_cli_is_unlimited_by_default(monkeypatch, workspace_settings):
    # CLI has no default cap — sending many messages must not trip rate_limited.
    # Use a dummy in-process CLI plugin via the existing CLI registration.
    monkeypatch.setattr(settings, "OUTBOUND_AUTOSEND_CHANNELS", "cli")
    # No OUTBOUND_SEND_LIMITS_JSON override -> cli stays unlimited.

    cli_channel = _RecordingChannel("cli")
    channels_mod._active_channels["cli"] = cli_channel
    try:
        for _ in range(30):
            result = await mcp_mod.send_message("cli:operator", "x")
            assert result.get("status") != "error", result
        assert len(cli_channel.sent) == 30
    finally:
        channels_mod._active_channels.pop("cli", None)


async def test_non_cli_channel_uses_default_limits(monkeypatch, fake_channel):
    # Without an explicit JSON override, non-cli channels fall back to the default profile
    # (per_minute=5). The 6th send within the minute must be rate-limited.
    for _ in range(5):
        result = await mcp_mod.send_message("fake:abc", "ping")
        assert result.get("status") != "error", result

    blocked = await mcp_mod.send_message("fake:abc", "ping")
    assert blocked.get("status") == "error"
    assert blocked.get("type") == "rate_limited"


async def test_empty_limits_dict_treats_channel_as_unlimited(monkeypatch, fake_channel):
    monkeypatch.setattr(settings, "OUTBOUND_SEND_LIMITS_JSON", '{"fake": {}}')
    for _ in range(50):
        result = await mcp_mod.send_message("fake:abc", "many")
        assert result.get("status") != "error", result
    assert len(fake_channel.sent) == 50
