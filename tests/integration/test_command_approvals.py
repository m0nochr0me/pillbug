"""execute_command allowlist + draft/commit approval flow (plan P0 #2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import mcp as mcp_mod
from app.core.config import settings
from app.runtime.approvals import approval_store


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    # The store caches per-base_dir; ensure tests start with a fresh in-memory map.
    approval_store._cache.clear()
    approval_store._loaded_dir = None
    return settings


@pytest.fixture
def dashboard_token(workspace_settings, monkeypatch):
    token = "test-dashboard-token-32characters"
    monkeypatch.setattr(settings, "DASHBOARD_BEARER_TOKEN", SecretStr(token))
    return token


@pytest.fixture
def control_client(workspace_settings):
    return TestClient(mcp_mod.mcp_app)


class TestExecuteCommandAllowlist:
    async def test_empty_allowlist_denies_everything(self, workspace_settings):
        result = await mcp_mod.execute_command("ls", directory=".")
        assert result["status"] == "error"
        assert result["type"] == "denied"
        assert result["details"]["reason"] == "command_not_on_allowlist"
        assert "draft_command" in result["next_valid_actions"]

    async def test_allowlist_match_executes(self, monkeypatch, workspace_settings):
        monkeypatch.setattr(settings, "EXECUTE_COMMAND_ALLOWLIST", r"^echo .*$")
        result = await mcp_mod.execute_command("echo hello", directory=".")
        assert "status" not in result or result.get("status") != "error", result
        assert result["exit_code"] == 0
        assert result["stdout"].strip() == "hello"

    async def test_allowlist_uses_fullmatch(self, monkeypatch, workspace_settings):
        # Pattern `echo .*` must fullmatch — `rm -rf` does not.
        monkeypatch.setattr(settings, "EXECUTE_COMMAND_ALLOWLIST", r"^echo .*$")
        result = await mcp_mod.execute_command("rm -rf /tmp/anything", directory=".")
        assert result["status"] == "error"
        assert result["type"] == "denied"


class TestDraftCommandTool:
    async def test_draft_requires_justification(self, workspace_settings):
        result = await mcp_mod.draft_command("rm -rf /tmp/x", justification="   ")
        assert result["status"] == "error"
        assert result["type"] == "invalid_arguments"

    async def test_draft_returns_id_and_persists(self, workspace_settings):
        result = await mcp_mod.draft_command(
            "rm -rf /tmp/something",
            justification="Cleanup of leftover artifact from previous run.",
        )
        assert result["status"] == "approval_required"
        draft_id = result["draft_id"]
        assert draft_id

        record = await approval_store.get(draft_id)
        assert record is not None
        assert record.command == "rm -rf /tmp/something"
        assert record.status == "pending"

        persisted = approval_store.base_dir / f"{draft_id}.json"
        assert persisted.is_file()
        payload = json.loads(persisted.read_text(encoding="utf-8"))
        assert payload["command"] == "rm -rf /tmp/something"


class TestRunApprovedCommand:
    async def test_run_unknown_draft_returns_not_found(self, workspace_settings):
        result = await mcp_mod.run_approved_command("does-not-exist")
        assert result["status"] == "error"
        assert result["type"] == "not_found"

    async def test_run_pending_draft_returns_approval_required(self, workspace_settings):
        draft_result = await mcp_mod.draft_command(
            "echo deferred",
            justification="needs operator sign-off",
        )
        draft_id = draft_result["draft_id"]

        result = await mcp_mod.run_approved_command(draft_id)
        assert result["status"] == "error"
        assert result["type"] == "approval_required"

    async def test_approve_and_run_succeeds_once(self, workspace_settings):
        draft_result = await mcp_mod.draft_command(
            "echo approved",
            justification="needs operator sign-off",
        )
        draft_id = draft_result["draft_id"]

        await approval_store.approve(draft_id, decided_by="dashboard")

        result = await mcp_mod.run_approved_command(draft_id)
        assert result.get("exit_code") == 0
        assert result["stdout"].strip() == "approved"
        assert result["approval"]["status"] == "used"

        # Second run is single-shot
        replay = await mcp_mod.run_approved_command(draft_id)
        assert replay["status"] == "error"
        assert replay["type"] == "already_used"

    async def test_denied_draft_cannot_be_redeemed(self, workspace_settings):
        draft_result = await mcp_mod.draft_command(
            "echo nope",
            justification="needs operator sign-off",
        )
        draft_id = draft_result["draft_id"]

        await approval_store.deny(draft_id, decided_by="dashboard")

        result = await mcp_mod.run_approved_command(draft_id)
        assert result["status"] == "error"
        assert result["type"] == "approval_required"


class TestApprovalControlEndpoints:
    async def test_approve_without_bearer_returns_401(self, dashboard_token, control_client, workspace_settings):
        draft_result = await mcp_mod.draft_command(
            "echo control-no-auth",
            justification="for control endpoint test",
        )
        draft_id = draft_result["draft_id"]

        response = control_client.post(f"/control/approvals/{draft_id}/approve", json={})
        assert response.status_code == 401

    async def test_approve_with_bearer_succeeds(self, dashboard_token, control_client, workspace_settings):
        draft_result = await mcp_mod.draft_command(
            "echo via-control",
            justification="for control endpoint test",
        )
        draft_id = draft_result["draft_id"]

        response = control_client.post(
            f"/control/approvals/{draft_id}/approve",
            json={"comment": "looks fine"},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["action"] == "approvals.approve"
        assert body["details"]["status"] == "approved"
        assert body["details"]["comment"] == "looks fine"

        record = await approval_store.get(draft_id)
        assert record is not None
        assert record.status == "approved"
        assert record.decided_by == "control"

    async def test_approve_already_decided_returns_409(self, dashboard_token, control_client, workspace_settings):
        draft_result = await mcp_mod.draft_command(
            "echo already",
            justification="for control endpoint test",
        )
        draft_id = draft_result["draft_id"]

        await approval_store.approve(draft_id, decided_by="dashboard")

        response = control_client.post(
            f"/control/approvals/{draft_id}/approve",
            json={},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 409

    async def test_approve_unknown_draft_returns_404(self, dashboard_token, control_client, workspace_settings):
        response = control_client.post(
            "/control/approvals/no-such-draft/approve",
            json={},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 404

    async def test_deny_endpoint_works(self, dashboard_token, control_client, workspace_settings):
        draft_result = await mcp_mod.draft_command(
            "echo deny-me",
            justification="for control endpoint test",
        )
        draft_id = draft_result["draft_id"]

        response = control_client.post(
            f"/control/approvals/{draft_id}/deny",
            json={"comment": "no thanks"},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        record = await approval_store.get(draft_id)
        assert record is not None
        assert record.status == "denied"
