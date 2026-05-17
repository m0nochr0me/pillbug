"""Compaction failure rollback (plan P1 #10)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from app.core import ai as ai_mod
from app.core.config import settings
from app.schema.ai import ChatSessionUsageTotals


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli")
    monkeypatch.setattr(settings, "CHANNEL_PLUGIN_FACTORIES", "")
    (settings.WORKSPACE_ROOT / "AGENTS.md").write_text("# AGENTS.md\n", encoding="utf-8")
    return settings


def _seed_session_with_history(session):
    history = [
        types.Content(role="user", parts=[types.Part.from_text(text="hello")]),
        types.Content(role="model", parts=[types.Part.from_text(text="hi there")]),
    ]
    session._chat = session._service.ai_client.aio.chats.create(
        model=settings.GEMINI_MODEL,
        history=[content.model_dump(mode="json") for content in history],
    )
    session._usage_totals = ChatSessionUsageTotals(
        prompt_token_count=42, candidates_token_count=17, total_token_count=59
    )


class TestSnapshotAndRestore:
    async def test_snapshot_captures_deep_copy(self, workspace_settings):
        service = ai_mod.GeminiChatService()
        session = service.create_session("cli:c1:u1")
        _seed_session_with_history(session)

        snapshot_history, snapshot_totals = session.snapshot_for_compaction()
        assert [c.parts[0].text for c in snapshot_history] == ["hello", "hi there"]
        assert snapshot_totals.prompt_token_count == 42

        # Mutate the live session — snapshot must remain intact.
        await session.replace_history_with_summary("erased")
        assert [c.parts[0].text for c in snapshot_history] == ["hello", "hi there"]

    async def test_restore_reverts_chat_history_and_totals(self, workspace_settings):
        service = ai_mod.GeminiChatService()
        session = service.create_session("cli:c1:u1")
        _seed_session_with_history(session)

        snapshot = session.snapshot_for_compaction()

        await session.replace_history_with_summary("placeholder summary text")
        post_summary_history = session._chat.get_history(curated=True)
        assert len(post_summary_history) == 1
        assert session._usage_totals.prompt_token_count == 0

        await session.restore_from_snapshot(snapshot)
        restored_history = session._chat.get_history(curated=True)
        assert [c.parts[0].text for c in restored_history] == ["hello", "hi there"]
        assert session._usage_totals.prompt_token_count == 42


class TestLoopRollbackOnFailure:
    async def test_replace_history_failure_triggers_restore_and_rollback_event(self, workspace_settings, monkeypatch):
        from app.runtime.loop import ApplicationLoop
        from app.runtime.pipeline import InboundProcessingPipeline

        service = ai_mod.GeminiChatService()
        loop = ApplicationLoop(chat_service=service, channels=[], pipeline=InboundProcessingPipeline())

        session_key = "cli:c1:u1"
        session = service.create_session(session_key)
        _seed_session_with_history(session)
        loop._sessions[session_key] = session

        # Drive auto-summarize into the compress branch.
        monkeypatch.setattr(settings, "SESSION_SUMMARIZATION", "compress")
        monkeypatch.setattr(settings, "SESSION_SUMMARIZATION_THRESHOLD", 1)

        # Stub send_message: return a non-empty compression summary so we reach replace.
        send_message_mock = AsyncMock(return_value=ai_mod.ChatResponse(text="ok summary", usage_metadata=None))
        monkeypatch.setattr(session, "send_message", send_message_mock)

        replace_mock = AsyncMock(side_effect=RuntimeError("simulated replace failure"))
        monkeypatch.setattr(session, "replace_history_with_summary", replace_mock)

        restore_mock = AsyncMock()
        monkeypatch.setattr(session, "restore_from_snapshot", restore_mock)

        emitted_events: list[dict] = []

        async def _capture_event(*, event_type, source, message, data=None, level="info"):
            emitted_events.append({"event_type": event_type, "level": level, "message": message, "data": data or {}})

        monkeypatch.setattr(loop, "_send_inbound_response", AsyncMock(return_value=True))
        from app.core import telemetry as telemetry_mod

        monkeypatch.setattr(telemetry_mod.runtime_telemetry, "record_event", _capture_event)

        from datetime import UTC, datetime

        from app.schema.messages import InboundBatch, InboundMessage

        inbound = InboundMessage(
            channel_name="cli",
            conversation_id="c1",
            user_id="u1",
            text="trigger compaction",
            received_at=datetime.now(UTC),
            metadata={},
        )
        batch = InboundBatch(
            session_key=session_key,
            channel_name="cli",
            conversation_id="c1",
            user_id="u1",
            received_at=datetime.now(UTC),
            messages=(inbound,),
        )

        # Total token count must exceed threshold.
        monkeypatch.setattr(session, "total_token_count", lambda: 1000)

        await loop._maybe_auto_summarize_session(channel=None, batch=batch, session=session)

        restore_mock.assert_awaited_once()
        rolled_back = [e for e in emitted_events if e["event_type"] == "session.summarization.rolled-back"]
        failed = [e for e in emitted_events if e["event_type"] == "session.summarization.failed"]
        assert len(rolled_back) == 1
        assert rolled_back[0]["level"] == "warning"
        assert "simulated replace failure" in rolled_back[0]["data"]["cause"]
        assert len(failed) == 1  # outer except still emits failed
