"""Session-scoped MCP client lifecycle (plan P1 #7)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import ai as ai_mod
from app.core.config import settings


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli")
    monkeypatch.setattr(settings, "CHANNEL_PLUGIN_FACTORIES", "")
    return settings


class _StubMcpClient:
    """A minimal stand-in for fastmcp.Client implementing the async-context protocol."""

    def __init__(self, *, fail_first_enter: bool = False, session_id: str | None = "stub-mcp") -> None:
        self.enter_calls = 0
        self.exit_calls = 0
        self._fail_first_enter = fail_first_enter
        self.transport = SimpleNamespace(get_session_id=lambda: session_id)
        self.session = SimpleNamespace(name="stub-mcp")

    async def __aenter__(self):
        self.enter_calls += 1
        if self._fail_first_enter and self.enter_calls == 1:
            raise ConnectionError("transport refused")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exit_calls += 1
        return False


@pytest.fixture
def service(workspace_settings):
    return ai_mod.GeminiChatService()


class TestEnsureMcpClientOpen:
    async def test_opens_once_per_session(self, monkeypatch, service):
        stub = _StubMcpClient()
        monkeypatch.setattr(ai_mod.GeminiChatSession, "_build_mcp_client", staticmethod(lambda: stub))

        session = service.create_session("cli:c1:u1")
        client_a = await session._ensure_mcp_client_open()
        client_b = await session._ensure_mcp_client_open()
        client_c = await session._ensure_mcp_client_open()

        assert client_a is stub and client_b is stub and client_c is stub
        assert stub.enter_calls == 1  # idempotent: only one transport connect

    async def test_reconnect_on_connection_error(self, monkeypatch, service):
        first_stub = _StubMcpClient(fail_first_enter=True)
        second_stub = _StubMcpClient()
        builders = iter([first_stub, second_stub])
        monkeypatch.setattr(
            ai_mod.GeminiChatSession,
            "_build_mcp_client",
            staticmethod(lambda: next(builders)),
        )

        session = service.create_session("cli:c1:u1")
        client = await session._ensure_mcp_client_open()
        assert client is second_stub
        assert first_stub.enter_calls == 1  # failed
        assert second_stub.enter_calls == 1  # succeeded on rebuild

    async def test_aclose_resets_state_and_allows_reopen(self, monkeypatch, service):
        first_stub = _StubMcpClient()
        monkeypatch.setattr(
            ai_mod.GeminiChatSession,
            "_build_mcp_client",
            staticmethod(lambda: first_stub),
        )

        session = service.create_session("cli:c1:u1")
        await session._ensure_mcp_client_open()
        assert session._mcp_client_opened is True
        await session.aclose()
        assert session._mcp_client_opened is False
        assert first_stub.exit_calls == 1

        # Re-opening after close works (uses the same client instance since builder returned it once)
        await session._ensure_mcp_client_open()
        assert first_stub.enter_calls == 2

    async def test_aclose_is_noop_when_never_opened(self, monkeypatch, service):
        stub = _StubMcpClient()
        monkeypatch.setattr(ai_mod.GeminiChatSession, "_build_mcp_client", staticmethod(lambda: stub))
        session = service.create_session("cli:c1:u1")
        await session.aclose()
        assert stub.exit_calls == 0


class TestLoopClosesSessionsOnClear:
    async def test_clear_session_closes_prior_mcp_client(self, monkeypatch, service):
        # Pre-populate the loop's session map with a session whose aclose() we can observe.
        from app.runtime.loop import ApplicationLoop
        from app.runtime.pipeline import InboundProcessingPipeline

        loop = ApplicationLoop(chat_service=service, channels=[], pipeline=InboundProcessingPipeline())

        previous = service.create_session("cli:c1:u1")
        previous.aclose = AsyncMock()
        loop._sessions["cli:c1:u1"] = previous

        # Register a state entry so _resolve_session_key can find the session.
        from datetime import UTC, datetime

        from app.runtime.loop import _SessionTelemetryState

        loop._session_state_by_key["cli:c1:u1"] = _SessionTelemetryState(
            session_key="cli:c1:u1",
            channel_name="cli",
            conversation_id="c1",
            user_id="u1",
            created_at=datetime.now(UTC),
        )

        stub = _StubMcpClient()
        monkeypatch.setattr(ai_mod.GeminiChatSession, "_build_mcp_client", staticmethod(lambda: stub))

        await loop.clear_session("cli:c1:u1")

        previous.aclose.assert_awaited_once()
        # The replaced session is fresh; we don't open it in this test.
