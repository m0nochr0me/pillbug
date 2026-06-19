"""System-instruction layout favors cache reuse (plan P1 #5)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core import ai as ai_mod
from app.core.ai import service as ai_service_mod
from app.core.config import settings
from app.runtime.session_binding import bind_runtime_session_todo_snapshot
from app.schema.todo import TodoItem, TodoListSnapshot


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli")
    monkeypatch.setattr(settings, "CHANNEL_PLUGIN_FACTORIES", "")
    (settings.WORKSPACE_ROOT / "AGENTS.md").write_text("# AGENTS.md\nstable instructions\n", encoding="utf-8")
    return settings


@pytest.fixture
def service(workspace_settings):
    return ai_mod.GeminiChatService()


async def test_system_instruction_excludes_todo_snapshot(service, workspace_settings):
    bind_runtime_session_todo_snapshot(
        "cli:default:user",
        TodoListSnapshot(items=[TodoItem(id="1", title="probe-todo-line", status="not-started")]),
    )
    instruction = await service.build_system_instruction()
    assert instruction is not None
    assert "probe-todo-line" not in instruction
    assert "Current session todo list" not in instruction


async def test_system_instruction_is_fully_stable_across_turns(service, workspace_settings):
    # base_context (the per-turn datetime) moved to the user turn, so the WHOLE system
    # instruction is now byte-identical across turns — the cached prefix never shifts.
    first = await service.build_system_instruction()
    second = await service.build_system_instruction()
    assert first is not None and second is not None
    assert hashlib.sha256(first.encode()).hexdigest() == hashlib.sha256(second.encode()).hexdigest()


async def test_system_instruction_excludes_base_context(service, workspace_settings):
    # The volatile base_context (datetime/workspace/channels) must NOT be in the system
    # instruction — it lives in the user turn so the cached prefix stays frozen. The stable
    # agents_md content is still present.
    instruction = await service.build_system_instruction()
    assert instruction is not None
    assert "stable instructions" in instruction
    assert "datetime:" not in instruction
    assert "available_channels:" not in instruction


async def test_message_parts_prepend_base_context_then_todo_snapshot(service, workspace_settings):
    session_id = "cli:plan-test:user"
    bind_runtime_session_todo_snapshot(
        session_id,
        TodoListSnapshot(items=[TodoItem(id="1", title="finish refactor", status="in-progress")]),
    )
    session = service.create_session(session_id)
    parts = await session._build_message_parts("hello", message_metadata=None, channel_name="cli")
    # base_context (volatile datetime) leads, then the todo snapshot, then the user text.
    assert len(parts) == 3
    assert "datetime:" in (parts[0].text or "")
    todo_text = parts[1].text or ""
    assert "Current plan state" in todo_text
    assert "finish refactor" in todo_text
    assert parts[2].text == "hello"


async def test_message_parts_keep_base_context_when_todo_empty(service, workspace_settings):
    session_id = "cli:empty-plan:user"
    bind_runtime_session_todo_snapshot(session_id, None)
    session = service.create_session(session_id)
    parts = await session._build_message_parts("hello", message_metadata=None, channel_name="cli")
    # No todo snapshot, but base_context still leads the user turn.
    assert len(parts) == 2
    assert "datetime:" in (parts[0].text or "")
    assert parts[1].text == "hello"


async def test_channel_memos_iterated_in_sorted_order(monkeypatch, service, workspace_settings):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "zeta,alpha,mid")
    captured: list[str] = []

    def _fake_get_plugin(name, *, create=False):
        captured.append(name)
        return None  # short-circuits the memo branch; we only care about iteration order

    monkeypatch.setattr(ai_service_mod, "get_channel_plugin", _fake_get_plugin)
    await service.get_channel_instruction_memos()
    assert captured == ["alpha", "mid", "zeta"]
