"""Rehydration bundle after compress-mode compaction (plan P1 #9)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import types

from app.core import ai as ai_mod
from app.core.config import settings
from app.runtime import session_binding
from app.runtime.approvals import approval_store, outbound_draft_store
from app.runtime.loop import ApplicationLoop
from app.runtime.pipeline import InboundProcessingPipeline
from app.runtime.session_binding import (
    bind_runtime_session_todo_snapshot,
    get_runtime_session_loaded_skills,
    record_runtime_session_skill_load,
)
from app.schema.todo import TodoItem, TodoListSnapshot
from app.util.rehydration import RehydrationBundle, render_rehydration_text, summarize_tool_observation


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path, monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli")
    monkeypatch.setattr(settings, "CHANNEL_PLUGIN_FACTORIES", "")
    (settings.WORKSPACE_ROOT / "AGENTS.md").write_text("# AGENTS.md\n", encoding="utf-8")
    approval_store._cache.clear()
    approval_store._loaded_dir = None
    outbound_draft_store._cache.clear()
    outbound_draft_store._loaded_dir = None
    session_binding._runtime_session_loaded_skills.clear()
    session_binding._runtime_session_todo_snapshots.clear()
    session_binding._mcp_runtime_sessions.clear()
    return settings


class TestRenderRehydrationText:
    def test_empty_bundle_renders_none(self):
        assert render_rehydration_text(RehydrationBundle()) is None

    def test_bundle_with_plan_only(self):
        bundle = RehydrationBundle(
            todo_snapshot=TodoListSnapshot(
                items=[TodoItem(id="1", title="finish refactor", status="in-progress")],
                explanation="resume after compact",
            )
        )
        text = render_rehydration_text(bundle)
        assert text is not None
        assert "Active plan:" in text
        assert "finish refactor" in text
        assert "resume after compact" in text
        assert "RUNTIME REHYDRATION" in text

    def test_bundle_renders_all_sections(self):
        bundle = RehydrationBundle(
            todo_snapshot=TodoListSnapshot(items=[TodoItem(id="1", title="A", status="completed")]),
            loaded_skill_names=("alpha", "beta"),
            pending_command_approvals=("draft-1",),
            pending_outbound_drafts=("out-2",),
            recent_tool_observations=("read_file: ...",),
        )
        text = render_rehydration_text(bundle)
        assert text is not None
        for marker in (
            "Active plan",
            "Skills already loaded",
            "alpha, beta",
            "Pending command approvals",
            "draft-1",
            "Pending outbound drafts",
            "out-2",
            "Most recent tool observations",
            "read_file: ...",
        ):
            assert marker in text


def test_summarize_tool_observation_caps_length():
    payload = "X" * 1000
    summary = summarize_tool_observation(payload, max_chars=120)
    assert len(summary) <= 120
    assert summary.endswith("…truncated")


class TestSessionToolObservationWalk:
    def test_collect_recent_tool_observations_pulls_function_responses(self, workspace_settings, monkeypatch):
        service = ai_mod.GeminiChatService()
        session = service.create_session("cli:c1:u1")

        history = [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="hi")],
            ),
            types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(name="read_file", args={"path": "x"}),
                    )
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="read_file",
                            response={"path": "x", "content": "hello"},
                        ),
                    )
                ],
            ),
        ]

        def fake_get_history(curated: bool = True):  # noqa: ARG001
            return [content.model_dump(mode="json") for content in history]

        monkeypatch.setattr(session._chat, "get_history", fake_get_history)

        observations = session.collect_recent_tool_observations(max_count=5, max_chars=200)
        assert len(observations) == 1
        assert observations[0].startswith("read_file:")


class TestBuildRehydrationBundle:
    async def test_bundle_collects_state(self, workspace_settings):
        service = ai_mod.GeminiChatService()
        loop = ApplicationLoop(chat_service=service, channels=[], pipeline=InboundProcessingPipeline())

        session_key = "cli:c1:u1"
        bind_runtime_session_todo_snapshot(
            session_key,
            TodoListSnapshot(items=[TodoItem(id="1", title="post-compact", status="not-started")]),
        )
        record_runtime_session_skill_load(session_key, "alpha")
        record_runtime_session_skill_load(session_key, "beta")

        command_draft = await approval_store.create_draft(
            command="ls",
            justification="for test",
            source=session_key,
        )
        outbound_draft = await outbound_draft_store.create(
            kind="send_message",
            channel="fake",
            target="abc",
            message="hello",
            source=session_key,
        )

        # Different-session draft must NOT appear in the bundle.
        await approval_store.create_draft(command="ls", justification="for test", source="other:session:user")

        session = service.create_session(session_key)
        bundle = await loop._build_rehydration_bundle(session, session_key)

        assert bundle.todo_snapshot is not None
        assert bundle.todo_snapshot.items[0].title == "post-compact"
        assert bundle.loaded_skill_names == ("alpha", "beta")
        assert command_draft.id in bundle.pending_command_approvals
        assert outbound_draft.id in bundle.pending_outbound_drafts


class TestReplaceHistoryWithSummaryAppendsRehydration:
    async def test_rehydration_turn_appended_after_summary(self, workspace_settings):
        service = ai_mod.GeminiChatService()
        session = service.create_session("cli:c1:u1")

        bundle = RehydrationBundle(
            todo_snapshot=TodoListSnapshot(items=[TodoItem(id="1", title="continue", status="not-started")]),
            loaded_skill_names=("alpha",),
        )

        await session.replace_history_with_summary("summary text", rehydration=bundle)

        history = session._chat.get_history(curated=True)
        assert len(history) == 2
        summary_text = history[0].parts[0].text
        assert "summary text" in summary_text
        rehydration_text = history[1].parts[0].text
        assert "RUNTIME REHYDRATION" in rehydration_text
        assert "continue" in rehydration_text
        assert "alpha" in rehydration_text

    async def test_no_rehydration_turn_when_bundle_empty(self, workspace_settings):
        service = ai_mod.GeminiChatService()
        session = service.create_session("cli:c1:u1")
        await session.replace_history_with_summary("summary text", rehydration=RehydrationBundle())
        history = session._chat.get_history(curated=True)
        assert len(history) == 1


class TestSkillLoadHook:
    async def test_read_file_records_skill_load(self, workspace_settings):
        from app import mcp as mcp_mod
        from app.runtime.session_binding import bind_mcp_session_to_runtime_session

        skill_dir = settings.WORKSPACE_ROOT / "skills" / "alpha"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: alpha\ndescription: x\n---\n", encoding="utf-8")

        session_key = "cli:c1:u1"
        bind_mcp_session_to_runtime_session("mcp-1", session_key)

        result = await mcp_mod.read_file("skills/alpha/SKILL.md", ctx=SimpleNamespace(session_id="mcp-1"))
        assert result.get("content")
        assert get_runtime_session_loaded_skills(session_key) == ("alpha",)

    async def test_non_skill_reads_do_not_record(self, workspace_settings):
        from app import mcp as mcp_mod
        from app.runtime.session_binding import bind_mcp_session_to_runtime_session

        (settings.WORKSPACE_ROOT / "notes.txt").write_text("hello", encoding="utf-8")
        session_key = "cli:c1:u2"
        bind_mcp_session_to_runtime_session("mcp-2", session_key)

        await mcp_mod.read_file("notes.txt", ctx=SimpleNamespace(session_id="mcp-2"))
        assert get_runtime_session_loaded_skills(session_key) == ()
