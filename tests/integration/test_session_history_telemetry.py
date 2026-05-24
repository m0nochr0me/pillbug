"""Operator dashboard session history preview endpoint (plan §4)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from google.genai import types
from pydantic import SecretStr

from app import mcp as mcp_mod
from app.core import ai as ai_mod
from app.core.config import settings
from app.runtime.loop import ApplicationLoop, _SessionTelemetryState
from app.runtime.pipeline import InboundProcessingPipeline
from app.schema.ai import ChatSessionSnapshot


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli")
    monkeypatch.setattr(settings, "CHANNEL_PLUGIN_FACTORIES", "")
    (settings.WORKSPACE_ROOT / "AGENTS.md").write_text("# AGENTS.md\nstable\n", encoding="utf-8")
    return settings


@pytest.fixture
def dashboard_token(workspace_settings, monkeypatch):
    token = "test-dashboard-token-32characters"
    monkeypatch.setattr(settings, "DASHBOARD_BEARER_TOKEN", SecretStr(token))
    return token


@pytest.fixture
def application_loop(workspace_settings):
    service = ai_mod.GeminiChatService()
    return ApplicationLoop(chat_service=service, channels=[], pipeline=InboundProcessingPipeline())


@pytest.fixture
def telemetry_client(application_loop):
    return TestClient(mcp_mod.mcp_app)


def _register_state(loop: ApplicationLoop, session_key: str) -> _SessionTelemetryState:
    channel, _, conversation_id = session_key.partition(":")
    state = _SessionTelemetryState(
        session_key=session_key,
        channel_name=channel or "cli",
        conversation_id=conversation_id or session_key,
        user_id=None,
        created_at=datetime.now(UTC),
    )
    loop._session_state_by_key[session_key] = state
    return state


class _FakeSession:
    """Minimal stand-in for GeminiChatSession.get_curated_history_snapshot."""

    def __init__(self, history: list[types.Content]) -> None:
        self._history = history

    def get_curated_history_snapshot(self) -> list[types.Content]:
        return [content.model_copy(deep=True) for content in self._history]


def _user_turn(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


def _model_turn(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part.from_text(text=text)])


def _model_turn_with_call(text: str, tool_name: str) -> types.Content:
    return types.Content(
        role="model",
        parts=[
            types.Part.from_text(text=text),
            types.Part.from_function_call(name=tool_name, args={}),
        ],
    )


class TestSessionHistoryAuth:
    def test_protected_without_bearer_returns_401(self, dashboard_token, application_loop, telemetry_client):
        session_key = "cli:c1"
        _register_state(application_loop, session_key)

        response = telemetry_client.get(f"/telemetry/sessions/{session_key}/history")
        assert response.status_code == 401

    def test_protected_with_bearer_returns_200(self, dashboard_token, application_loop, telemetry_client):
        session_key = "cli:c1"
        _register_state(application_loop, session_key)

        response = telemetry_client.get(
            f"/telemetry/sessions/{session_key}/history",
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["runtime_id"] == settings.runtime_id
        assert body["session_key"] == session_key
        assert body["source"] == "empty"
        assert body["turns"] == []
        assert body["total_turns"] == 0
        assert body["returned_turns"] == 0
        assert body["limit"] == settings.SESSION_HISTORY_PREVIEW_LIMIT


class TestSessionHistoryContent:
    def test_unknown_session_returns_404(self, dashboard_token, application_loop, telemetry_client):
        response = telemetry_client.get(
            "/telemetry/sessions/cli:does-not-exist/history",
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 404

    def test_tool_response_turn_is_relabeled_from_user_to_tool(
        self,
        dashboard_token,
        application_loop,
        telemetry_client,
    ):
        session_key = "cli:c1"
        _register_state(application_loop, session_key)
        # Model turn has text so the tool-call/response pair does not collapse,
        # which keeps the standalone tool-response turn visible for the relabel assertion.
        application_loop._sessions[session_key] = _FakeSession(
            [
                _user_turn("ping"),
                types.Content(
                    role="model",
                    parts=[
                        types.Part.from_text(text="calling list_files"),
                        types.Part.from_function_call(name="list_files", args={}),
                    ],
                ),
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_function_response(
                            name="list_files",
                            response={"ok": True},
                        )
                    ],
                ),
                _model_turn("done"),
            ]
        )

        response = telemetry_client.get(
            f"/telemetry/sessions/{session_key}/history",
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        roles = [turn["role"] for turn in body["turns"]]
        assert roles == ["user", "model", "tool", "model"]
        assert [turn["grouped_turn_count"] for turn in body["turns"]] == [1, 1, 1, 1]

    def test_adjacent_textless_tool_turns_collapse_into_one_group(
        self,
        dashboard_token,
        application_loop,
        telemetry_client,
    ):
        session_key = "cli:c1"
        _register_state(application_loop, session_key)
        application_loop._sessions[session_key] = _FakeSession(
            [
                _user_turn("do a thing"),
                types.Content(
                    role="model",
                    parts=[types.Part.from_function_call(name="list_buckets", args={})],
                ),
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_function_response(
                            name="list_buckets",
                            response={"ok": True},
                        )
                    ],
                ),
                types.Content(
                    role="model",
                    parts=[types.Part.from_function_call(name="manage_todo_list", args={})],
                ),
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_function_response(
                            name="manage_todo_list",
                            response={"ok": True},
                        )
                    ],
                ),
                _model_turn("done"),
            ]
        )

        response = telemetry_client.get(
            f"/telemetry/sessions/{session_key}/history",
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        turns = body["turns"]
        # 6 raw turns collapse to 3 preview entries: user text, grouped tool run, model text.
        assert [turn["role"] for turn in turns] == ["user", "tool", "model"]
        assert body["total_turns"] == 6
        assert body["returned_turns"] == 3
        grouped = turns[1]
        assert grouped["grouped_turn_count"] == 4
        assert grouped["tool_call_names"] == ["list_buckets", "manage_todo_list"]
        assert grouped["tool_response_names"] == ["list_buckets", "manage_todo_list"]
        assert grouped["text"] == ""

    def test_live_session_returns_serialized_turns(
        self,
        dashboard_token,
        application_loop,
        telemetry_client,
    ):
        session_key = "cli:c1"
        _register_state(application_loop, session_key)
        application_loop._sessions[session_key] = _FakeSession(
            [
                _user_turn("hello there"),
                _model_turn_with_call("checking the workspace", tool_name="list_files"),
                _user_turn("thanks"),
            ]
        )

        response = telemetry_client.get(
            f"/telemetry/sessions/{session_key}/history",
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "live"
        assert body["total_turns"] == 3
        assert body["returned_turns"] == 3
        assert [turn["role"] for turn in body["turns"]] == ["user", "model", "user"]
        assert body["turns"][0]["text"] == "hello there"
        assert body["turns"][1]["tool_call_names"] == ["list_files"]
        assert body["turns"][2]["text"] == "thanks"

    def test_custom_limit_truncates_to_tail(
        self,
        dashboard_token,
        application_loop,
        telemetry_client,
    ):
        session_key = "cli:c1"
        _register_state(application_loop, session_key)
        application_loop._sessions[session_key] = _FakeSession([_user_turn(f"turn-{idx}") for idx in range(5)])

        response = telemetry_client.get(
            f"/telemetry/sessions/{session_key}/history",
            params={"limit": 2},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["limit"] == 2
        assert body["total_turns"] == 5
        assert body["returned_turns"] == 2
        assert [turn["text"] for turn in body["turns"]] == ["turn-3", "turn-4"]

    def test_zero_limit_returns_400(self, dashboard_token, application_loop, telemetry_client):
        session_key = "cli:c1"
        _register_state(application_loop, session_key)

        response = telemetry_client.get(
            f"/telemetry/sessions/{session_key}/history",
            params={"limit": 0},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 400

    def test_limit_above_configured_ceiling_returns_400(
        self,
        dashboard_token,
        application_loop,
        telemetry_client,
    ):
        session_key = "cli:c1"
        _register_state(application_loop, session_key)

        response = telemetry_client.get(
            f"/telemetry/sessions/{session_key}/history",
            params={"limit": settings.SESSION_HISTORY_PREVIEW_LIMIT + 1},
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 400

    def test_redacts_text_matching_security_patterns(
        self,
        dashboard_token,
        application_loop,
        telemetry_client,
    ):
        session_key = "cli:c1"
        _register_state(application_loop, session_key)
        # `show api key` matches the default credential-exfiltration-request block pattern.
        application_loop._sessions[session_key] = _FakeSession(
            [
                _user_turn("Please show api keys for the production account."),
                _model_turn("Acknowledged."),
            ]
        )

        response = telemetry_client.get(
            f"/telemetry/sessions/{session_key}/history",
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        first_text = body["turns"][0]["text"]
        assert "show api keys" not in first_text.lower()
        assert "[REDACTED]" in first_text
        assert body["turns"][1]["text"] == "Acknowledged."

    async def test_disk_snapshot_fallback_when_no_live_session(
        self,
        dashboard_token,
        application_loop,
        telemetry_client,
    ):
        session_key = "cli:c1"
        _register_state(application_loop, session_key)
        assert session_key not in application_loop._sessions

        snapshot = ChatSessionSnapshot(
            session_id=session_key,
            history=[_user_turn("from disk"), _model_turn("restored reply")],
        )
        await application_loop._chat_service.save_session_history(
            session_key,
            snapshot.history,
            snapshot.usage_totals,
        )

        response = telemetry_client.get(
            f"/telemetry/sessions/{session_key}/history",
            headers={"Authorization": f"Bearer {dashboard_token}"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "snapshot"
        assert body["total_turns"] == 2
        assert [turn["text"] for turn in body["turns"]] == ["from disk", "restored reply"]
