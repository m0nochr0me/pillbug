"""read_file emits skill.loaded telemetry and surfaces loaded skills (plan P2 #18)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import mcp as mcp_mod
from app.core.config import settings
from app.core.telemetry import runtime_telemetry
from app.runtime import session_binding
from app.runtime.loop import ApplicationLoop, _SessionTelemetryState
from app.schema.messages import InboundMessage, OutboundAttachment


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    session_binding._mcp_runtime_sessions.clear()
    session_binding._runtime_session_loaded_skills.clear()
    runtime_telemetry._events.clear()
    return settings


class _StubCtx:
    def __init__(self, session_id: str = "mcp-session-skill-test") -> None:
        self.session_id = session_id


def _seed_skill_md(workspace: Path, skill_name: str, body: str = "---\nname: x\ndescription: y\n---\n") -> Path:
    skill_dir = workspace / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    target = skill_dir / "SKILL.md"
    target.write_text(body, encoding="utf-8")
    return target


async def test_skill_load_emits_event_once_per_session(workspace_settings):
    _seed_skill_md(settings.WORKSPACE_ROOT, "alpha")
    session_binding.bind_mcp_session_to_runtime_session("mcp-1", "cli:conv:user")
    ctx = _StubCtx("mcp-1")

    await mcp_mod.read_file("skills/alpha/SKILL.md", ctx=ctx)
    await mcp_mod.read_file("skills/alpha/SKILL.md", ctx=ctx)

    skill_events = [event for event in runtime_telemetry._events if event.event_type == "skill.loaded"]
    assert len(skill_events) == 1
    assert skill_events[0].data["skill_name"] == "alpha"
    assert skill_events[0].data["runtime_session_key"] == "cli:conv:user"
    assert session_binding.get_runtime_session_loaded_skills("cli:conv:user") == ("alpha",)


async def test_skill_load_skipped_when_no_runtime_session_bound(workspace_settings):
    _seed_skill_md(settings.WORKSPACE_ROOT, "beta")
    ctx = _StubCtx("orphan-session")  # no binding registered

    await mcp_mod.read_file("skills/beta/SKILL.md", ctx=ctx)

    assert [event for event in runtime_telemetry._events if event.event_type == "skill.loaded"] == []
    assert session_binding._runtime_session_loaded_skills == {}


async def test_non_skill_path_does_not_emit_event(workspace_settings):
    # SKILL.md outside skills/<name>/ must not trigger the hook.
    stray = settings.WORKSPACE_ROOT / "docs"
    stray.mkdir(parents=True)
    (stray / "SKILL.md").write_text("not a real skill\n", encoding="utf-8")

    session_binding.bind_mcp_session_to_runtime_session("mcp-2", "cli:conv:user")
    ctx = _StubCtx("mcp-2")

    await mcp_mod.read_file("docs/SKILL.md", ctx=ctx)

    assert [event for event in runtime_telemetry._events if event.event_type == "skill.loaded"] == []
    assert session_binding.get_runtime_session_loaded_skills("cli:conv:user") == ()


class _StubChatService:
    async def send_message(self, *args, **kwargs):
        raise AssertionError("stub chat service should not be invoked")

    async def reset_session(self, session_key):
        return None

    def set_outbound_injection_handler(self, handler):
        return None


class _RecordingChannel:
    destination_kind = "explicit"

    def __init__(self, name: str = "cli") -> None:
        self.name = name

    async def listen(self):  # pragma: no cover - never iterated in this test
        if False:
            yield  # type: ignore[unreachable]

    async def send_message(
        self,
        conversation_id: str,
        message_text: str,
        metadata: dict[str, object] | None = None,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        return None

    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        return None

    @asynccontextmanager
    async def response_presence(self, inbound_message: InboundMessage):
        yield

    async def close(self) -> None:
        return None


async def test_sessions_telemetry_surfaces_loaded_skill_names(workspace_settings):
    loop = ApplicationLoop(
        chat_service=_StubChatService(),  # type: ignore[arg-type]
        channels=[_RecordingChannel("cli")],
    )
    session_key = "cli:conv:operator"
    loop._session_state_by_key[session_key] = _SessionTelemetryState(
        session_key=session_key,
        channel_name="cli",
        conversation_id="conv",
        user_id="operator",
        created_at=datetime.now(UTC),
    )
    session_binding.record_runtime_session_skill_load(session_key, "gamma")
    session_binding.record_runtime_session_skill_load(session_key, "delta")

    snapshot = await loop.describe_sessions_telemetry()
    by_key = {entry.session_key: entry for entry in snapshot.sessions}
    assert by_key[session_key].loaded_skill_names == ("delta", "gamma")
