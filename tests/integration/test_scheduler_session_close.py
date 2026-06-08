"""Regression: scheduled runs must close their per-run chat session.

Each scheduled run builds a fresh `GeminiChatSession` that lazily opens its own MCP
transport on `send_message`. The scheduler previously dropped the session without
calling `aclose()`, leaking the connection's file descriptors on every run until the
process hit `OSError: [Errno 24] No file descriptors available`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.runtime.scheduler import AgentTaskScheduler
from app.schema.ai import ChatResponse
from app.schema.tasks import AgentTaskDefinition, CronTaskSchedule


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    return isolated_settings


class _RecordingSession:
    def __init__(self, response_text: str = '{"action": "continue", "message": "ok"}') -> None:
        self._response_text = response_text
        self.closed = False
        self.send_calls = 0

    async def send_message(self, message: str, max_remote_calls: int | None = None) -> ChatResponse:
        self.send_calls += 1
        return ChatResponse(text=self._response_text)

    async def aclose(self) -> None:
        self.closed = True


class _FailingSession(_RecordingSession):
    async def send_message(self, message: str, max_remote_calls: int | None = None) -> ChatResponse:
        self.send_calls += 1
        raise RuntimeError("boom")


class _FakeChatService:
    def __init__(self, session: _RecordingSession) -> None:
        self._session = session
        self.reset_calls = 0
        self.restore_calls = 0

    async def reset_session(self, session_id: str) -> _RecordingSession:
        self.reset_calls += 1
        return self._session

    async def restore_session(self, session_id: str) -> _RecordingSession:
        self.restore_calls += 1
        return self._session

    def render_prompt_text(self, prompt_name: str, **context: Any) -> str:
        return f"rendered:{prompt_name}"


def _cron_definition(*, clean_session: bool) -> AgentTaskDefinition:
    return AgentTaskDefinition(
        task_id="task-close",
        name="close-example",
        prompt="do work",
        schedule=CronTaskSchedule(expression="0 * * * *"),
        clean_session=clean_session,
    )


async def _run_once(scheduler: AgentTaskScheduler, definition: AgentTaskDefinition) -> None:
    await scheduler._run_task_definition(
        definition=definition,
        revision=definition.revision,
        behavior=None,
        allow_disabled=True,
        apply_schedule_effects=False,
        trigger="control",
    )


class TestSchedulerClosesSession:
    async def test_clean_session_run_closes_session(self, workspace_settings):
        session = _RecordingSession()
        service = _FakeChatService(session)
        scheduler = AgentTaskScheduler(chat_service=service)  # type: ignore[arg-type]

        await _run_once(scheduler, _cron_definition(clean_session=True))

        assert service.reset_calls == 1
        assert session.send_calls == 1
        assert session.closed is True

    async def test_restored_session_run_closes_session(self, workspace_settings):
        session = _RecordingSession()
        service = _FakeChatService(session)
        scheduler = AgentTaskScheduler(chat_service=service)  # type: ignore[arg-type]

        await _run_once(scheduler, _cron_definition(clean_session=False))

        assert service.restore_calls == 1
        assert session.closed is True

    async def test_session_closed_when_send_message_raises(self, workspace_settings):
        session = _FailingSession()
        service = _FakeChatService(session)
        scheduler = AgentTaskScheduler(chat_service=service)  # type: ignore[arg-type]

        with pytest.raises(RuntimeError, match="boom"):
            await _run_once(scheduler, _cron_definition(clean_session=True))

        assert session.closed is True
