"""Draft/commit split for outbound sends (plan P0 #3)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import mcp as mcp_mod
from app.core.config import settings
from app.runtime import channels as channels_mod
from app.runtime.approvals import outbound_draft_store
from app.schema.messages import InboundMessage, OutboundAttachment


class _RecordingChannel:
    name = "fake"
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
        self.sent.append(
            {
                "conversation_id": conversation_id,
                "message": message_text,
                "metadata": metadata,
                "attachments": attachments,
            }
        )

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
    return settings


@pytest.fixture
def fake_channel(workspace_settings, monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli,fake")
    monkeypatch.setattr(settings, "OUTBOUND_AUTOSEND_CHANNELS", "cli")
    channel = _RecordingChannel("fake")
    channels_mod._active_channels["fake"] = channel
    try:
        yield channel
    finally:
        channels_mod._active_channels.pop("fake", None)


@pytest.fixture
def dashboard_token(workspace_settings, monkeypatch):
    token = "test-dashboard-token-32characters"
    monkeypatch.setattr(settings, "DASHBOARD_BEARER_TOKEN", SecretStr(token))
    return token


@pytest.fixture
def control_client(workspace_settings):
    return TestClient(mcp_mod.mcp_app)


class TestSendMessageGating:
    async def test_autosend_channel_dispatches_immediately(self, monkeypatch, fake_channel):
        monkeypatch.setattr(settings, "OUTBOUND_AUTOSEND_CHANNELS", "cli,fake")
        result = await mcp_mod.send_message("fake:abc", "auto-dispatched")
        assert "status" not in result or result.get("status") != "error", result
        assert result.get("channel") == "fake"
        assert result.get("conversation_id") == "abc"
        assert len(fake_channel.sent) == 1
        assert fake_channel.sent[0]["message"] == "auto-dispatched"

    async def test_non_autosend_channel_returns_requires_approval(self, fake_channel):
        result = await mcp_mod.send_message("fake:abc", "hello")
        assert result["status"] == "requires_approval"
        assert result["channel"] == "fake"
        assert result["target"] == "abc"
        assert result["draft_id"]
        # No call to the channel until operator commits
        assert fake_channel.sent == []

    async def test_unknown_channel_returns_not_found(self, workspace_settings):
        result = await mcp_mod.send_message("nope_channel", "hello")
        assert result["status"] == "error"
        assert result["type"] == "not_found"


class TestDraftOutboundMessageTool:
    async def test_draft_records_pending(self, fake_channel):
        result = await mcp_mod.draft_outbound_message("fake:xyz", "queued message")
        assert result["status"] == "draft_created"
        draft_id = result["draft_id"]
        record = await outbound_draft_store.get(draft_id)
        assert record is not None
        assert record.status == "pending"
        assert record.kind.value == "send_message"
        assert record.channel == "fake"
        assert record.target == "xyz"
        persisted = outbound_draft_store.base_dir / f"{draft_id}.json"
        payload = json.loads(persisted.read_text(encoding="utf-8"))
        assert payload["message"] == "queued message"

    async def test_draft_with_attachment_creates_send_file_kind(self, fake_channel):
        attachment = settings.WORKSPACE_ROOT / "note.txt"
        attachment.write_text("hello attachment", encoding="utf-8")
        result = await mcp_mod.draft_outbound_message(
            "fake:xyz",
            "see attached",
            attachment_path="note.txt",
            attachment_caption="here you go",
        )
        assert result["status"] == "draft_created"
        draft_id = result["draft_id"]
        record = await outbound_draft_store.get(draft_id)
        assert record is not None
        assert record.kind.value == "send_file"
        assert record.attachment is not None
        assert record.attachment.caption == "here you go"

    async def test_draft_rejects_empty_when_no_attachment(self, fake_channel):
        result = await mcp_mod.draft_outbound_message("fake:xyz", "")
        assert result["status"] == "error"
        assert result["type"] == "invalid_arguments"


class TestCommitOutboundMessageTool:
    async def test_unknown_draft_returns_not_found(self, workspace_settings):
        result = await mcp_mod.commit_outbound_message("missing")
        assert result["status"] == "error"
        assert result["type"] == "not_found"

    async def test_non_autosend_returns_approval_required(self, fake_channel):
        draft_result = await mcp_mod.draft_outbound_message("fake:xyz", "queued")
        result = await mcp_mod.commit_outbound_message(draft_result["draft_id"])
        assert result["status"] == "error"
        assert result["type"] == "approval_required"
        assert fake_channel.sent == []

    async def test_autosend_channel_can_commit(self, monkeypatch, fake_channel):
        monkeypatch.setattr(settings, "OUTBOUND_AUTOSEND_CHANNELS", "cli,fake")
        draft_result = await mcp_mod.draft_outbound_message("fake:xyz", "auto-sent")
        result = await mcp_mod.commit_outbound_message(draft_result["draft_id"])
        assert result.get("channel") == "fake"
        assert result.get("conversation_id") == "xyz"
        assert len(fake_channel.sent) == 1
        assert fake_channel.sent[0]["message"] == "auto-sent"

        # Second commit is single-shot
        replay = await mcp_mod.commit_outbound_message(draft_result["draft_id"])
        assert replay["status"] == "error"
        assert replay["type"] == "already_used"


class TestOutboundDraftControlEndpoints:
    async def test_commit_without_bearer_returns_401(self, dashboard_token, control_client, fake_channel):
        draft_result = await mcp_mod.draft_outbound_message("fake:xyz", "control test")
        response = control_client.post(
            f"/control/drafts/{draft_result['draft_id']}/commit",
            json={},
        )
        assert response.status_code == 401

    async def test_commit_with_bearer_dispatches(self, dashboard_token, control_client, fake_channel):
        draft_result = await mcp_mod.draft_outbound_message("fake:xyz", "via control")
        draft_id = draft_result["draft_id"]

        response = control_client.post(
            f"/control/drafts/{draft_id}/commit",
            json={"comment": "ok to send"},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["action"] == "drafts.commit"
        assert body["details"]["dispatch_failed"] is False
        assert len(fake_channel.sent) == 1
        assert fake_channel.sent[0]["message"] == "via control"

        record = await outbound_draft_store.get(draft_id)
        assert record is not None
        assert record.status == "committed"
        assert record.decided_by == "control"

    async def test_commit_already_committed_returns_409(self, dashboard_token, control_client, fake_channel):
        draft_result = await mcp_mod.draft_outbound_message("fake:xyz", "already")
        draft_id = draft_result["draft_id"]
        await outbound_draft_store.commit(draft_id, decided_by="autosend")

        response = control_client.post(
            f"/control/drafts/{draft_id}/commit",
            json={},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 409

    async def test_commit_unknown_returns_404(self, dashboard_token, control_client, workspace_settings):
        response = control_client.post(
            "/control/drafts/no-such-draft/commit",
            json={},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 404

    async def test_discard_marks_draft_discarded(self, dashboard_token, control_client, fake_channel):
        draft_result = await mcp_mod.draft_outbound_message("fake:xyz", "discard me")
        draft_id = draft_result["draft_id"]

        response = control_client.post(
            f"/control/drafts/{draft_id}/discard",
            json={"comment": "not now"},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        record = await outbound_draft_store.get(draft_id)
        assert record is not None
        assert record.status == "discarded"

        # No dispatch happened
        assert fake_channel.sent == []

        # commit_outbound_message now refuses to dispatch a discarded draft
        result = await mcp_mod.commit_outbound_message(draft_id)
        assert result["status"] == "error"
        assert result["type"] == "denied"
