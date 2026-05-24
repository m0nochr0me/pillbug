"""Operator dashboard drafts telemetry endpoint (plan §1)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import mcp as mcp_mod
from app.core.config import settings
from app.runtime.approvals import approval_store, outbound_draft_store


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    # Both stores cache per-base_dir; ensure tests start with fresh in-memory maps.
    outbound_draft_store._cache.clear()
    outbound_draft_store._loaded_dir = None
    approval_store._cache.clear()
    approval_store._loaded_dir = None
    return settings


@pytest.fixture
def dashboard_token(workspace_settings, monkeypatch):
    token = "test-dashboard-token-32characters"
    monkeypatch.setattr(settings, "DASHBOARD_BEARER_TOKEN", SecretStr(token))
    return token


@pytest.fixture
def telemetry_client(workspace_settings):
    return TestClient(mcp_mod.mcp_app)


class TestDraftsTelemetryAuth:
    def test_protected_without_bearer_returns_401(self, dashboard_token, telemetry_client):
        response = telemetry_client.get("/telemetry/drafts")
        assert response.status_code == 401

    def test_protected_with_bearer_returns_200(self, dashboard_token, telemetry_client):
        response = telemetry_client.get(
            "/telemetry/drafts",
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["runtime_id"] == settings.runtime_id
        assert body["status_filter"] == "pending"
        assert body["outbound"] == []
        assert body["command"] == []


class TestDraftsTelemetryContent:
    async def test_pending_filter_excludes_committed_and_used(
        self,
        dashboard_token,
        telemetry_client,
    ):
        pending = await outbound_draft_store.create(
            kind="send_message",
            channel="telegram",
            target="123",
            message="pending one",
            source="session-key",
        )
        committed = await outbound_draft_store.create(
            kind="send_message",
            channel="telegram",
            target="123",
            message="already done",
            source="session-key",
        )
        await outbound_draft_store.commit(committed.id, decided_by="autosend")

        pending_cmd = await approval_store.create_draft(
            command="ls -la",
            justification="inspect workspace",
            source="session-key",
        )
        used_cmd = await approval_store.create_draft(
            command="echo done",
            justification="finalize",
            source="session-key",
        )
        await approval_store.approve(used_cmd.id, decided_by="control")
        await approval_store.consume(used_cmd.id)

        response = telemetry_client.get(
            "/telemetry/drafts",
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()

        outbound_ids = [record["id"] for record in body["outbound"]]
        command_ids = [record["id"] for record in body["command"]]
        assert outbound_ids == [pending.id]
        assert command_ids == [pending_cmd.id]

    async def test_status_all_includes_terminal_records(
        self,
        dashboard_token,
        telemetry_client,
    ):
        pending = await outbound_draft_store.create(
            kind="send_message",
            channel="telegram",
            target="abc",
            message="pending",
            source="session-key",
        )
        discarded = await outbound_draft_store.create(
            kind="send_message",
            channel="telegram",
            target="abc",
            message="rejected",
            source="session-key",
        )
        await outbound_draft_store.discard(discarded.id, decided_by="control")

        denied_cmd = await approval_store.create_draft(
            command="rm -rf /",
            justification="probably bad",
            source="session-key",
        )
        await approval_store.deny(denied_cmd.id, decided_by="control")

        response = telemetry_client.get(
            "/telemetry/drafts",
            params={"status": "all"},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status_filter"] == "all"

        outbound_ids = {record["id"] for record in body["outbound"]}
        command_ids = {record["id"] for record in body["command"]}
        assert outbound_ids == {pending.id, discarded.id}
        assert command_ids == {denied_cmd.id}

    def test_unknown_status_returns_400(self, dashboard_token, telemetry_client):
        response = telemetry_client.get(
            "/telemetry/drafts",
            params={"status": "garbage"},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 400
