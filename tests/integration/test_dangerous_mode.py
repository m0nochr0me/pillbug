"""PB_DANGEROUSLY_APPROVE_EVERYTHING bypass mode (plan P1 #22)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import mcp as mcp_mod
from app.core.config import settings
from app.core.telemetry import runtime_telemetry
from app.runtime import channels as channels_mod
from app.runtime.approvals import approval_store, outbound_draft_store
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
    approval_store._cache.clear()
    approval_store._loaded_dir = None
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
def dangerous_mode(monkeypatch, workspace_settings):
    monkeypatch.setattr(settings, "DANGEROUSLY_APPROVE_EVERYTHING", True)
    return settings


@pytest.fixture
def dashboard_token(workspace_settings, monkeypatch):
    token = "test-dashboard-token-32characters"
    monkeypatch.setattr(settings, "DASHBOARD_BEARER_TOKEN", SecretStr(token))
    return token


class TestExecuteCommandBypass:
    async def test_off_allowlist_command_runs_when_flag_on(self, dangerous_mode):
        # Empty allowlist would normally deny.
        result = await mcp_mod.execute_command("echo dangerous", directory=".")
        assert result.get("status") != "error", result
        assert result["exit_code"] == 0
        assert result["stdout"].strip() == "dangerous"


class TestSendMessageBypass:
    async def test_non_autosend_channel_dispatches_when_flag_on(self, dangerous_mode, fake_channel):
        result = await mcp_mod.send_message("fake:abc", "yolo send")
        assert result.get("status") != "error", result
        assert result.get("channel") == "fake"
        assert result.get("conversation_id") == "abc"
        assert len(fake_channel.sent) == 1
        assert fake_channel.sent[0]["message"] == "yolo send"


class TestDraftCommandBypass:
    async def test_draft_command_auto_approves_when_flag_on(self, dangerous_mode):
        result = await mcp_mod.draft_command(
            "echo auto-approved",
            justification="bypass test",
        )
        assert result["status"] == "approval_required"
        draft_id = result["draft_id"]
        record = await approval_store.get(draft_id)
        assert record is not None
        assert record.status == "approved"
        assert record.decided_by == "dangerous_mode"
        assert "run_approved_command" in result["next_valid_actions"]
        assert "wait_for_operator" not in result["next_valid_actions"]

    async def test_run_approved_command_succeeds_without_http_approval(self, dangerous_mode):
        draft_result = await mcp_mod.draft_command(
            "echo end-to-end",
            justification="bypass test",
        )
        draft_id = draft_result["draft_id"]

        run_result = await mcp_mod.run_approved_command(draft_id)
        assert run_result.get("status") != "error", run_result
        assert run_result["exit_code"] == 0
        assert run_result["stdout"].strip() == "end-to-end"
        assert run_result["approval"]["decided_by"] == "dangerous_mode"


class TestTelemetrySurface:
    def test_metadata_exposes_approvals_bypassed_when_on(self, dangerous_mode):
        metadata = runtime_telemetry.metadata()
        assert metadata.approvals_bypassed is True

    def test_metadata_reflects_flag_off(self, workspace_settings, monkeypatch):
        monkeypatch.setattr(settings, "DANGEROUSLY_APPROVE_EVERYTHING", False)
        metadata = runtime_telemetry.metadata()
        assert metadata.approvals_bypassed is False

    def test_telemetry_runtime_endpoint_surfaces_flag(self, dangerous_mode, dashboard_token):
        client = TestClient(mcp_mod.mcp_app)
        response = client.get(
            "/telemetry/runtime",
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["metadata"]["approvals_bypassed"] is True
