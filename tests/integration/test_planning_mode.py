"""Runtime planning mode tools, gate, and control endpoint (plan P2 #11)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import mcp as mcp_mod
from app.core.config import settings
from app.runtime.approvals import approval_store, outbound_draft_store
from app.runtime.loop import ApplicationLoop, _SessionTelemetryState
from app.runtime.session_binding import bind_mcp_session_to_runtime_session
from app.runtime.session_mode import (
    SessionMode,
    clear_session_mode,
    get_planning_state,
    get_session_mode,
)
from app.runtime.session_mode import (
    enter_planning_mode as registry_enter_planning,
)
from app.schema.messages import InboundMessage, OutboundAttachment

_RUNTIME_SESSION_KEY = "cli:planning-conv"
_MCP_SESSION_ID = "mcp-planning-session"


def _bound_ctx(mcp_session_id: str = _MCP_SESSION_ID) -> SimpleNamespace:
    return SimpleNamespace(session_id=mcp_session_id)


@pytest.fixture(autouse=True)
def _clean_registries(isolated_settings, tmp_workspace: Path):
    approval_store._cache.clear()
    approval_store._loaded_dir = None
    outbound_draft_store._cache.clear()
    outbound_draft_store._loaded_dir = None
    clear_session_mode(_RUNTIME_SESSION_KEY)
    bind_mcp_session_to_runtime_session(_MCP_SESSION_ID, _RUNTIME_SESSION_KEY)
    yield
    clear_session_mode(_RUNTIME_SESSION_KEY)


@pytest.fixture
def dashboard_token(monkeypatch):
    token = "test-dashboard-token-32characters"
    monkeypatch.setattr(settings, "DASHBOARD_BEARER_TOKEN", SecretStr(token))
    return token


@pytest.fixture
def control_client():
    return TestClient(mcp_mod.mcp_app)


class _StubChatService:
    def set_outbound_injection_handler(self, handler) -> None:
        self._handler = handler


class _RecordingChannel:
    name = "cli"
    destination_kind = "explicit"

    def __init__(self, name: str = "cli") -> None:
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
        self.sent.append({"conversation_id": conversation_id, "message": message_text})

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

    async def close(self) -> None:
        return None


@pytest.fixture
def bound_loop():
    loop = ApplicationLoop(
        chat_service=_StubChatService(),  # type: ignore[arg-type]
        channels=[_RecordingChannel("cli")],
    )
    loop._session_state_by_key[_RUNTIME_SESSION_KEY] = _SessionTelemetryState(
        session_key=_RUNTIME_SESSION_KEY,
        channel_name="cli",
        conversation_id="planning-conv",
        user_id=None,
        created_at=datetime.now(UTC),
    )
    mcp_mod.bind_application_loop(loop)
    yield loop
    mcp_mod.bind_application_loop(None)


class TestPlanningModeToolDispatch:
    async def test_enter_planning_mode_sets_registry(self):
        result = await mcp_mod.enter_planning_mode(
            "Investigate slow telegram replies",
            scope="diagnose latency without sending anything",
            ctx=_bound_ctx(),
        )
        assert result["status"] == "ok"
        assert result["mode"] == "planning"
        assert result["objective"] == "Investigate slow telegram replies"
        assert result["scope"] == "diagnose latency without sending anything"
        assert "exit_planning_mode" in result["next_valid_actions"]

        assert get_session_mode(_RUNTIME_SESSION_KEY) is SessionMode.PLANNING
        state = get_planning_state(_RUNTIME_SESSION_KEY)
        assert state is not None
        assert state.source == "model"

    async def test_enter_planning_mode_requires_objective(self):
        result = await mcp_mod.enter_planning_mode("   ", ctx=_bound_ctx())
        assert result["status"] == "error"
        assert result["type"] == "invalid_arguments"

    async def test_enter_planning_mode_without_session_returns_permission_denied(self):
        # Bind nothing; unknown mcp session id has no runtime session.
        result = await mcp_mod.enter_planning_mode(
            "Some objective",
            ctx=SimpleNamespace(session_id="unbound-session-id"),
        )
        assert result["status"] == "error"
        assert result["type"] == "permission_denied"

    async def test_enter_then_exit_writes_artifact_and_clears_mode(self):
        await mcp_mod.enter_planning_mode("Rebuild auth handshake", ctx=_bound_ctx())

        plan_text = (
            "1. Read app/core/auth.py\n"
            "2. Compare with peer runtime behavior\n"
            "3. Draft a single replace_file_text patch\n"
            "Success: handshake test green, no other tests regressed."
        )
        result = await mcp_mod.exit_planning_mode(plan_text, ctx=_bound_ctx())
        assert result["status"] == "ok"
        assert result["mode"] == "normal"
        assert result["objective"] == "Rebuild auth handshake"

        plan_path_str = result["plan_path"]
        assert plan_path_str.startswith("plans/active/")
        artifact = settings.WORKSPACE_ROOT / plan_path_str
        assert artifact.is_file()
        body = artifact.read_text(encoding="utf-8")
        assert "session_key: cli:planning-conv" in body
        assert "source: model" in body
        assert "objective: Rebuild auth handshake" in body
        assert "Success: handshake test green" in body

        assert get_session_mode(_RUNTIME_SESSION_KEY) is SessionMode.NORMAL
        assert get_planning_state(_RUNTIME_SESSION_KEY) is None

    async def test_exit_planning_mode_when_not_planning_returns_conflict(self):
        result = await mcp_mod.exit_planning_mode("plan body", ctx=_bound_ctx())
        assert result["status"] == "error"
        assert result["type"] == "conflict"
        assert result["next_valid_actions"] == ["enter_planning_mode"]

    async def test_exit_planning_mode_requires_summary(self):
        await mcp_mod.enter_planning_mode("anything", ctx=_bound_ctx())
        result = await mcp_mod.exit_planning_mode("  ", ctx=_bound_ctx())
        assert result["status"] == "error"
        assert result["type"] == "invalid_arguments"


class TestPlanningModeGate:
    async def test_execute_command_blocked_while_planning(self, monkeypatch):
        # Allow `echo .*` so we can prove the gate runs BEFORE the allowlist check
        # (denied here is from planning mode, not from the allowlist).
        monkeypatch.setattr(settings, "EXECUTE_COMMAND_ALLOWLIST", r"^echo .*$")
        registry_enter_planning(_RUNTIME_SESSION_KEY, objective="planning")

        result = await mcp_mod.execute_command("echo hello", directory=".", ctx=_bound_ctx())
        assert result["status"] == "error"
        assert result["type"] == "denied"
        assert result["details"]["reason"] == "planning_mode_blocked"
        assert result["details"]["tool"] == "execute_command"
        assert "exit_planning_mode" in result["next_valid_actions"]

    async def test_send_message_blocked_while_planning(self):
        registry_enter_planning(_RUNTIME_SESSION_KEY, objective="planning")

        result = await mcp_mod.send_message("cli", "hi", ctx=_bound_ctx())
        assert result["status"] == "error"
        assert result["type"] == "denied"
        assert result["details"]["reason"] == "planning_mode_blocked"
        assert result["details"]["tool"] == "send_message"

    async def test_write_new_file_blocked_while_planning(self):
        registry_enter_planning(_RUNTIME_SESSION_KEY, objective="planning")

        result = await mcp_mod.write_new_file("a/b.txt", "data", ctx=_bound_ctx())
        assert result["status"] == "error"
        assert result["type"] == "denied"
        assert result["details"]["reason"] == "planning_mode_blocked"
        assert not (settings.WORKSPACE_ROOT / "a" / "b.txt").exists()

    async def test_manage_agent_task_create_blocked_but_list_allowed(self):
        registry_enter_planning(_RUNTIME_SESSION_KEY, objective="planning")

        blocked = await mcp_mod.manage_agent_task(
            action="create",
            name="x",
            prompt="y",
            schedule_type="delayed",
            delay_seconds=10,
            ctx=_bound_ctx(),
        )
        assert blocked["status"] == "error"
        assert blocked["type"] == "denied"
        assert blocked["details"]["reason"] == "planning_mode_blocked"
        assert blocked["details"]["tool"] == "manage_agent_task.create"

        # list is read-only and must remain available.
        listing = await mcp_mod.manage_agent_task(action="list", ctx=_bound_ctx())
        assert "status" not in listing or listing.get("status") != "error", listing

    async def test_gate_is_noop_when_ctx_unbound(self, monkeypatch):
        # No registry entry; gate is a no-op even if some other session is planning.
        monkeypatch.setattr(settings, "EXECUTE_COMMAND_ALLOWLIST", r"^echo .*$")
        registry_enter_planning(_RUNTIME_SESSION_KEY, objective="planning")

        # Call without ctx — pre-existing test pattern.
        result = await mcp_mod.execute_command("echo unbound", directory=".")
        assert result.get("exit_code") == 0

    async def test_draft_command_not_blocked_while_planning(self):
        """draft_command must remain usable so the model can stage proposals
        without leaving planning mode."""
        registry_enter_planning(_RUNTIME_SESSION_KEY, objective="planning")

        result = await mcp_mod.draft_command(
            "rm -rf /tmp/safe-target",
            justification="proposed cleanup pending operator review",
            ctx=_bound_ctx(),
        )
        assert result["status"] == "approval_required"
        assert "draft_id" in result


class TestPlanningModeControlEndpoint:
    async def test_no_bearer_returns_401(self, bound_loop, control_client, dashboard_token):
        response = control_client.post(
            f"/control/sessions/{_RUNTIME_SESSION_KEY}/planning-mode",
            json={"state": "planning", "objective": "investigate"},
        )
        assert response.status_code == 401

    async def test_unknown_session_returns_404(self, bound_loop, control_client, dashboard_token):
        response = control_client.post(
            "/control/sessions/no-such-conv/planning-mode",
            headers={"Authorization": f"Bearer {dashboard_token}"},
            json={"state": "planning", "objective": "investigate"},
        )
        assert response.status_code == 404

    async def test_planning_requires_objective(self, bound_loop, control_client, dashboard_token):
        response = control_client.post(
            f"/control/sessions/{_RUNTIME_SESSION_KEY}/planning-mode",
            headers={"Authorization": f"Bearer {dashboard_token}"},
            json={"state": "planning"},
        )
        assert response.status_code == 422

    async def test_operator_can_enter_and_exit(self, bound_loop, control_client, dashboard_token):
        enter_response = control_client.post(
            f"/control/sessions/{_RUNTIME_SESSION_KEY}/planning-mode",
            headers={"Authorization": f"Bearer {dashboard_token}"},
            json={"state": "planning", "objective": "operator-forced-investigation"},
        )
        assert enter_response.status_code == 200
        body = enter_response.json()
        assert body["action"] == "session.planning-mode"
        assert body["details"]["mode"] == "planning"
        assert get_session_mode(_RUNTIME_SESSION_KEY) is SessionMode.PLANNING
        state = get_planning_state(_RUNTIME_SESSION_KEY)
        assert state is not None
        assert state.source == "control-api"

        exit_response = control_client.post(
            f"/control/sessions/{_RUNTIME_SESSION_KEY}/planning-mode",
            headers={"Authorization": f"Bearer {dashboard_token}"},
            json={"state": "normal", "plan_summary": "Operator-approved abort."},
        )
        assert exit_response.status_code == 200
        exit_body = exit_response.json()
        assert exit_body["details"]["mode"] == "normal"
        plan_path = exit_body["details"]["plan_path"]
        artifact = settings.WORKSPACE_ROOT / plan_path
        assert artifact.is_file()
        body_text = artifact.read_text(encoding="utf-8")
        assert "source: control-api" in body_text
        assert "Operator-approved abort." in body_text
        assert get_session_mode(_RUNTIME_SESSION_KEY) is SessionMode.NORMAL

    async def test_exit_when_not_planning_returns_409(self, bound_loop, control_client, dashboard_token):
        response = control_client.post(
            f"/control/sessions/{_RUNTIME_SESSION_KEY}/planning-mode",
            headers={"Authorization": f"Bearer {dashboard_token}"},
            json={"state": "normal"},
        )
        assert response.status_code == 409

    async def test_exit_without_plan_summary_uses_default_text(self, bound_loop, control_client, dashboard_token):
        registry_enter_planning(_RUNTIME_SESSION_KEY, objective="operator-stuck")

        response = control_client.post(
            f"/control/sessions/{_RUNTIME_SESSION_KEY}/planning-mode",
            headers={"Authorization": f"Bearer {dashboard_token}"},
            json={"state": "normal"},
        )
        assert response.status_code == 200
        artifact = settings.WORKSPACE_ROOT / response.json()["details"]["plan_path"]
        assert "operator-cleared without plan summary" in artifact.read_text(encoding="utf-8")


class TestPlanningModeTelemetryAndClear:
    async def test_describe_sessions_surfaces_mode(self, bound_loop):
        registry_enter_planning(
            _RUNTIME_SESSION_KEY,
            objective="audit telegram traffic",
            scope="read-only inspection",
        )
        snapshot = await bound_loop.describe_sessions_telemetry()
        entries = {entry.session_key: entry for entry in snapshot.sessions}
        assert entries[_RUNTIME_SESSION_KEY].mode == "planning"
        assert entries[_RUNTIME_SESSION_KEY].planning_objective == "audit telegram traffic"

    async def test_clear_session_resets_planning_mode(self, bound_loop, monkeypatch):
        registry_enter_planning(_RUNTIME_SESSION_KEY, objective="planning")

        # reset_session is async; stub it to avoid Gemini setup.
        async def _fake_reset(session_key):
            return SimpleNamespace(aclose=_noop_async)

        async def _noop_async():
            return None

        monkeypatch.setattr(bound_loop._chat_service, "reset_session", _fake_reset, raising=False)
        await bound_loop.clear_session(_RUNTIME_SESSION_KEY)

        assert get_session_mode(_RUNTIME_SESSION_KEY) is SessionMode.NORMAL
        assert get_planning_state(_RUNTIME_SESSION_KEY) is None
