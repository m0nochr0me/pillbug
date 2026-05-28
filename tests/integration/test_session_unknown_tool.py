"""Recovery when the model calls a tool that doesn't exist.

google-genai's AFC loop raises a bare KeyError when the model emits a functionCall whose
name isn't in the declared tool set (a hallucinated tool — common with the local models
behind pillbug-genai-proxy). GeminiChatSession catches that, re-prompts the model naming the
missing tool plus the tools that actually exist, and lets it answer instead of crashing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from app.core import ai as ai_mod
from app.core.config import settings


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli")
    monkeypatch.setattr(settings, "CHANNEL_PLUGIN_FACTORIES", "")
    return settings


@pytest.fixture
def service(workspace_settings):
    return ai_mod.GeminiChatService()


def _text_parts(message: list[types.Part]) -> list[str]:
    return [part.text for part in message if getattr(part, "text", None)]


class _ChatFailingOnce:
    """Raises KeyError(name) on the first send (mimicking AFC's unguarded lookup), then succeeds."""

    def __init__(self, *, fail_name: str, result: object) -> None:
        self._fail_name: str | None = fail_name
        self._result = result
        self.sent_messages: list[object] = []

    async def send_message(self, *, message, config):
        self.sent_messages.append(message)
        if self._fail_name is not None:
            name, self._fail_name = self._fail_name, None
            raise KeyError(name)
        return self._result


async def test_unknown_tool_call_reprompts_with_available_tools(monkeypatch, service):
    session = service.create_session("cli:c1:u1")
    sentinel = SimpleNamespace(text="I don't have a weather tool, but Cebu is usually warm.")
    fake_chat = _ChatFailingOnce(fail_name="weather", result=sentinel)
    session._chat = fake_chat
    monkeypatch.setattr(session, "_list_available_tool_names", AsyncMock(return_value=["fetch_url", "read_file"]))

    original = [types.Part.from_text(text="Could you please check the weather in Cebu today")]
    result = await session._send_chat_message(message=original, config=SimpleNamespace())

    assert result is sentinel
    assert len(fake_chat.sent_messages) == 2

    retry_texts = _text_parts(fake_chat.sent_messages[1])
    # The original question is preserved so the model still has context for this turn.
    assert any("check the weather in Cebu" in text for text in retry_texts)
    # The appended note names the missing tool and the tools that actually exist.
    note = retry_texts[-1]
    assert "weather" in note
    assert "fetch_url" in note and "read_file" in note


async def test_unknown_tool_gives_up_after_max_nudges(monkeypatch, service):
    session = service.create_session("cli:c1:u1")
    hallucinated = iter(["weather", "stocks", "news", "horoscope"])

    class _AlwaysHallucinates:
        def __init__(self) -> None:
            self.calls = 0

        async def send_message(self, *, message, config):
            self.calls += 1
            raise KeyError(next(hallucinated))

    fake_chat = _AlwaysHallucinates()
    session._chat = fake_chat
    monkeypatch.setattr(session, "_list_available_tool_names", AsyncMock(return_value=["read_file"]))

    with pytest.raises(KeyError):
        await session._send_chat_message(message="hi", config=SimpleNamespace())

    # Initial send plus the bounded number of re-prompts, then it re-raises rather than looping.
    assert fake_chat.calls == 1 + ai_mod._UNKNOWN_TOOL_MAX_NUDGES


async def test_non_string_keyerror_is_not_swallowed(monkeypatch, service):
    session = service.create_session("cli:c1:u1")

    class _ChatRaisingTupleKey:
        async def send_message(self, *, message, config):
            raise KeyError(("not", "a", "tool", "name"))

    session._chat = _ChatRaisingTupleKey()
    list_tools = AsyncMock(return_value=["read_file"])
    monkeypatch.setattr(session, "_list_available_tool_names", list_tools)

    with pytest.raises(KeyError):
        await session._send_chat_message(message="hi", config=SimpleNamespace())
    # A KeyError that isn't a plain tool-name string is re-raised untouched (no nudge attempted).
    list_tools.assert_not_awaited()
