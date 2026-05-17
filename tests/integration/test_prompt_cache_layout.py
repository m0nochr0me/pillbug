"""System-instruction layout favors cache reuse (plan P1 #5)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core import ai as ai_mod
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
    instruction = await service.build_system_instruction(channel_name="cli")
    assert instruction is not None
    assert "probe-todo-line" not in instruction
    assert "Current session todo list" not in instruction


async def test_system_instruction_prefix_is_stable_across_turns(service, workspace_settings):
    first = await service.build_system_instruction(channel_name="cli")
    second = await service.build_system_instruction(channel_name="cli")
    assert first is not None and second is not None

    # The only volatile region is the trailing base_context block (datetime + workspace etc.).
    # Everything before the first "---" separator at the end is the stable cached prefix.
    def stable_prefix(text: str) -> str:
        return text.rsplit("---", 2)[0]

    assert (
        hashlib.sha256(stable_prefix(first).encode()).hexdigest()
        == hashlib.sha256(stable_prefix(second).encode()).hexdigest()
    )


async def test_system_instruction_ends_with_base_context(service, workspace_settings):
    instruction = await service.build_system_instruction(channel_name="cli")
    assert instruction is not None
    # The volatile base_context (with the per-turn datetime) must come AFTER the stable
    # agents_md so the cached prefix covers agents_md / skills / memos.
    agents_md_pos = instruction.find("stable instructions")
    datetime_pos = instruction.find("datetime:")
    assert agents_md_pos >= 0 and datetime_pos > agents_md_pos


async def test_message_parts_prepend_todo_snapshot(service, workspace_settings):
    session_id = "cli:plan-test:user"
    bind_runtime_session_todo_snapshot(
        session_id,
        TodoListSnapshot(items=[TodoItem(id="1", title="finish refactor", status="in-progress")]),
    )
    session = service.create_session(session_id)
    parts = await session._build_message_parts("hello", message_metadata=None)
    assert len(parts) == 2
    todo_text = parts[0].text or ""
    assert "Current plan state" in todo_text
    assert "finish refactor" in todo_text
    assert parts[1].text == "hello"


async def test_message_parts_omit_todo_part_when_empty(service, workspace_settings):
    session_id = "cli:empty-plan:user"
    bind_runtime_session_todo_snapshot(session_id, None)
    session = service.create_session(session_id)
    parts = await session._build_message_parts("hello", message_metadata=None)
    assert len(parts) == 1
    assert parts[0].text == "hello"


async def test_channel_memos_iterated_in_sorted_order(monkeypatch, service, workspace_settings):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "zeta,alpha,mid")
    captured: list[str] = []

    def _fake_get_plugin(name, *, create=False):
        captured.append(name)
        return None  # short-circuits the memo branch; we only care about iteration order

    monkeypatch.setattr(ai_mod, "get_channel_plugin", _fake_get_plugin)
    await service.get_channel_instruction_memos()
    assert captured == ["alpha", "mid", "zeta"]
