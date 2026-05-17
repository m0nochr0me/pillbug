"""In-channel /yes /no /drafts runtime commands (plan P1 #21)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.runtime.approvals import approval_store, outbound_draft_store
from app.runtime.channels import ChannelPlugin
from app.runtime.loop import ApplicationLoop
from app.schema.messages import InboundBatch, InboundMessage, OutboundAttachment


class _RecordingChannel:
    name = "fake"
    destination_kind = "explicit"

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.responses: list[str] = []
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
        self.sent.append(
            {
                "conversation_id": conversation_id,
                "message": message_text,
                "metadata": metadata,
                "attachments": attachments,
            }
        )

    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        self.responses.append(response_text)

    @asynccontextmanager
    async def response_presence(self, inbound_message: InboundMessage):
        yield

    async def close(self) -> None:  # pragma: no cover - not exercised in these tests
        return None


class _StubChatService:
    """Bare-minimum stand-in: ApplicationLoop only touches set_outbound_injection_handler."""

    def set_outbound_injection_handler(self, handler) -> None:
        self._handler = handler


def _build_loop(channel: ChannelPlugin) -> ApplicationLoop:
    return ApplicationLoop(
        chat_service=_StubChatService(),  # type: ignore[arg-type]
        channels=[channel],
    )


def _batch_for(message_text: str, channel_name: str = "fake") -> InboundBatch:
    message = InboundMessage(
        channel_name=channel_name,
        conversation_id="conv-1",
        user_id="u-1",
        text=message_text,
    )
    return InboundBatch(messages=(message,))


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    approval_store._cache.clear()
    approval_store._loaded_dir = None
    outbound_draft_store._cache.clear()
    outbound_draft_store._loaded_dir = None
    return settings


@pytest.fixture
def fake_channel(workspace_settings) -> _RecordingChannel:
    return _RecordingChannel("fake")


class TestRecognizedCommand:
    def test_existing_commands_still_recognized(self, workspace_settings):
        loop = _build_loop(_RecordingChannel())
        assert loop._recognized_command("/clear") == ("/clear", "")
        assert loop._recognized_command("/usage") == ("/usage", "")
        assert loop._recognized_command("/summarize") == ("/summarize", "")

    def test_existing_command_with_argument_returns_none(self, workspace_settings):
        loop = _build_loop(_RecordingChannel())
        # /clear should be exact; trailing text means it isn't the command anymore.
        assert loop._recognized_command("/clear extra") is None

    def test_yes_extracts_draft_id(self, workspace_settings):
        loop = _build_loop(_RecordingChannel())
        assert loop._recognized_command("/yes abc123") == ("/yes", "abc123")
        assert loop._recognized_command("/no abc123") == ("/no", "abc123")

    def test_drafts_recognized_with_no_args(self, workspace_settings):
        loop = _build_loop(_RecordingChannel())
        assert loop._recognized_command("/drafts") == ("/drafts", "")

    def test_unknown_command_returns_none(self, workspace_settings):
        loop = _build_loop(_RecordingChannel())
        assert loop._recognized_command("/notacommand") is None
        assert loop._recognized_command("hello") is None


class TestYesCommand:
    async def test_unknown_draft_id_replies_not_found(self, fake_channel):
        loop = _build_loop(fake_channel)
        await loop._handle_command(_batch_for("/yes nonexistent"), fake_channel)
        assert any("draft not found" in r for r in fake_channel.responses)

    async def test_missing_argument_replies_usage(self, fake_channel):
        loop = _build_loop(fake_channel)
        await loop._handle_command(_batch_for("/yes"), fake_channel)
        assert any("Usage: /yes" in r for r in fake_channel.responses)

    async def test_yes_runs_command_draft_and_marks_used(self, monkeypatch, fake_channel):
        monkeypatch.setattr(settings, "EXECUTE_COMMAND_ALLOWLIST", "")
        draft = await approval_store.create_draft(
            command="echo hello-from-yes",
            justification="testing /yes",
            source="cli:conv-1:u-1",
            directory=".",
        )

        loop = _build_loop(fake_channel)
        await loop._handle_command(_batch_for(f"/yes {draft.id}"), fake_channel)

        record = await approval_store.get(draft.id)
        assert record is not None
        assert record.status == "used"

        joined_replies = "\n".join(fake_channel.responses)
        assert "echo hello-from-yes" in joined_replies
        assert "exit_code=0" in joined_replies

    async def test_yes_already_decided_is_noop(self, fake_channel):
        draft = await approval_store.create_draft(
            command="ls",
            justification="t",
            source="cli:conv-1:u-1",
            directory=".",
        )
        await approval_store.deny(draft.id, decided_by="prev-test")

        loop = _build_loop(fake_channel)
        await loop._handle_command(_batch_for(f"/yes {draft.id}"), fake_channel)

        record = await approval_store.get(draft.id)
        assert record is not None
        assert record.status == "denied"  # untouched
        assert any("already denied" in r for r in fake_channel.responses)

    async def test_yes_dispatches_outbound_draft(self, monkeypatch, fake_channel):
        from app.runtime import channels as channels_mod

        monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli,fake")
        monkeypatch.setattr(settings, "OUTBOUND_AUTOSEND_CHANNELS", "")
        channels_mod._active_channels["fake"] = fake_channel
        try:
            from app import mcp as mcp_mod

            draft_result = await mcp_mod.draft_outbound_message("fake:abc", "hello via yes")
            draft_id = draft_result["draft_id"]

            loop = _build_loop(fake_channel)
            await loop._handle_command(_batch_for(f"/yes {draft_id}"), fake_channel)
        finally:
            channels_mod._active_channels.pop("fake", None)

        record = await outbound_draft_store.get(draft_id)
        assert record is not None
        assert record.status == "committed"
        # The outbound recipient call landed exactly once
        assert len(fake_channel.sent) == 1
        assert fake_channel.sent[0]["message"] == "hello via yes"
        assert any("dispatch ok" in r for r in fake_channel.responses)


class TestNoCommand:
    async def test_no_denies_command_draft(self, fake_channel):
        draft = await approval_store.create_draft(
            command="echo nope",
            justification="t",
            source="cli:conv-1:u-1",
            directory=".",
        )
        loop = _build_loop(fake_channel)
        await loop._handle_command(_batch_for(f"/no {draft.id}"), fake_channel)

        record = await approval_store.get(draft.id)
        assert record is not None
        assert record.status == "denied"
        assert any(f"denied: {draft.id}" in r for r in fake_channel.responses)

    async def test_no_discards_outbound_draft(self, monkeypatch, fake_channel):
        from app.runtime import channels as channels_mod

        monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli,fake")
        monkeypatch.setattr(settings, "OUTBOUND_AUTOSEND_CHANNELS", "")
        channels_mod._active_channels["fake"] = fake_channel
        try:
            from app import mcp as mcp_mod

            draft_result = await mcp_mod.draft_outbound_message("fake:abc", "draft to discard")
            draft_id = draft_result["draft_id"]

            loop = _build_loop(fake_channel)
            await loop._handle_command(_batch_for(f"/no {draft_id}"), fake_channel)
        finally:
            channels_mod._active_channels.pop("fake", None)

        record = await outbound_draft_store.get(draft_id)
        assert record is not None
        assert record.status == "discarded"
        # Nothing was sent through the channel
        assert fake_channel.sent == []
        assert any(f"discarded: {draft_id}" in r for r in fake_channel.responses)


class TestDraftsListCommand:
    async def test_empty_returns_no_pending_drafts(self, fake_channel):
        loop = _build_loop(fake_channel)
        await loop._handle_command(_batch_for("/drafts"), fake_channel)
        assert fake_channel.responses == ["no pending drafts"]

    async def test_lists_both_kinds_with_cap(self, monkeypatch, fake_channel):
        from app.runtime import channels as channels_mod

        monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli,fake")
        monkeypatch.setattr(settings, "OUTBOUND_AUTOSEND_CHANNELS", "")
        channels_mod._active_channels["fake"] = fake_channel
        try:
            from app import mcp as mcp_mod

            # Two command drafts + two outbound drafts, plus enough extras to exercise the cap.
            cmd_a = await approval_store.create_draft(
                command="ls /tmp",
                justification="t",
                source="cli:conv-1:u-1",
                directory=".",
            )
            cmd_b = await approval_store.create_draft(
                command="echo hi",
                justification="t",
                source="cli:conv-1:u-1",
                directory=".",
            )
            outbound_one = await mcp_mod.draft_outbound_message("fake:abc", "first")
            outbound_two = await mcp_mod.draft_outbound_message("fake:abc", "second")

            for index in range(8):
                await approval_store.create_draft(
                    command=f"echo extra-{index}",
                    justification="t",
                    source="cli:conv-1:u-1",
                    directory=".",
                )

            loop = _build_loop(fake_channel)
            await loop._handle_command(_batch_for("/drafts"), fake_channel)
        finally:
            channels_mod._active_channels.pop("fake", None)

        assert len(fake_channel.responses) == 1
        body = fake_channel.responses[0]
        # Cap at 10; with 12 total we should see a `(10+)` indicator and 10 lines.
        assert body.startswith("pending drafts (10+):")
        lines = body.splitlines()[1:]
        assert len(lines) == 10
        # The first-created drafts (cmd_a, cmd_b, outbound_one, outbound_two) should appear oldest-first.
        assert any(cmd_a.id in line and line.startswith("command ") for line in lines)
        assert any(cmd_b.id in line and line.startswith("command ") for line in lines)
        assert any(outbound_one["draft_id"] in line and line.startswith("outbound ") for line in lines)
        assert any(outbound_two["draft_id"] in line and line.startswith("outbound ") for line in lines)
