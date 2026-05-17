"""TelemetryMiddleware emits matching tool.call.started/completed events (plan P2 #16)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastmcp.server.middleware.middleware import MiddlewareContext
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolRequestParams, TextContent

from app.core.telemetry import runtime_telemetry
from app.middleware.telemetry import TelemetryMiddleware
from app.runtime import session_binding


@dataclass
class _FakeContext:
    """Mimics fastmcp.Context just enough for TelemetryMiddleware."""

    session_id: str


@pytest.fixture
def fresh_telemetry():
    session_binding._mcp_runtime_sessions.clear()
    runtime_telemetry._events.clear()
    yield runtime_telemetry
    runtime_telemetry._events.clear()


def _make_context(
    *,
    tool_name: str,
    arguments: dict[str, Any] | None,
    fastmcp_context: _FakeContext | None,
) -> MiddlewareContext[CallToolRequestParams]:
    return MiddlewareContext(
        message=CallToolRequestParams(name=tool_name, arguments=arguments or {}),
        fastmcp_context=fastmcp_context,
        method="tools/call",
    )


def _success_result(payload: dict[str, Any]) -> ToolResult:
    return ToolResult(content=[TextContent(type="text", text="ok")], structured_content=payload)


async def test_emits_started_and_completed_with_matching_hash(fresh_telemetry):
    session_binding.bind_mcp_session_to_runtime_session("mcp-1", "cli:conv:user")

    async def call_next(ctx):
        return _success_result({"status": "ok", "stdout": "hello"})

    middleware = TelemetryMiddleware()
    context = _make_context(
        tool_name="execute_command",
        arguments={"command": "echo hello", "directory": "."},
        fastmcp_context=_FakeContext(session_id="mcp-1"),
    )

    await middleware.on_call_tool(context, call_next)

    started = [event for event in runtime_telemetry._events if event.event_type == "tool.call.started"]
    completed = [event for event in runtime_telemetry._events if event.event_type == "tool.call.completed"]
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0].data["tool_name"] == "execute_command"
    assert started[0].data["runtime_session_key"] == "cli:conv:user"
    assert started[0].data["args_hash"] == completed[0].data["args_hash"]
    assert completed[0].data["result_status"] == "ok"
    assert completed[0].data["duration_ms"] >= 0
    assert "error_type" not in completed[0].data


async def test_envelope_error_status_propagates_to_completed_event(fresh_telemetry):
    async def call_next(ctx):
        return _success_result({"status": "error", "type": "not_found", "message": "missing"})

    middleware = TelemetryMiddleware()
    context = _make_context(
        tool_name="read_file",
        arguments={"path": "does/not/exist.txt"},
        fastmcp_context=None,
    )

    await middleware.on_call_tool(context, call_next)

    completed = [event for event in runtime_telemetry._events if event.event_type == "tool.call.completed"]
    assert completed[0].data["result_status"] == "error"
    assert completed[0].level == "error"


async def test_exception_records_completed_event_with_error_type(fresh_telemetry):
    class _Boom(RuntimeError):
        pass

    async def call_next(ctx):
        raise _Boom("kaboom")

    middleware = TelemetryMiddleware()
    context = _make_context(
        tool_name="execute_command",
        arguments={"command": "x"},
        fastmcp_context=None,
    )

    with pytest.raises(_Boom):
        await middleware.on_call_tool(context, call_next)

    completed = [event for event in runtime_telemetry._events if event.event_type == "tool.call.completed"]
    assert completed[0].data["result_status"] == "exception"
    assert completed[0].data["error_type"] == "_Boom"
    assert completed[0].level == "error"


async def test_args_summary_redacts_secrets(fresh_telemetry):
    async def call_next(ctx):
        return _success_result({"status": "ok"})

    middleware = TelemetryMiddleware()
    context = _make_context(
        tool_name="fetch_url",
        arguments={"url": "https://example.com", "api_key": "sk-very-secret-xyz"},
        fastmcp_context=None,
    )

    await middleware.on_call_tool(context, call_next)

    started = [event for event in runtime_telemetry._events if event.event_type == "tool.call.started"]
    summary = started[0].data["args_summary"]
    assert "sk-very-secret-xyz" not in summary
    assert "[REDACTED]" in summary


async def test_telemetry_failure_does_not_block_tool(fresh_telemetry, monkeypatch):
    # If record_event throws, the middleware must not propagate the failure.
    async def boom(**kwargs):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(runtime_telemetry, "record_event", boom)

    async def call_next(ctx):
        return _success_result({"status": "ok"})

    middleware = TelemetryMiddleware()
    context = _make_context(tool_name="list_files", arguments={}, fastmcp_context=None)

    # No exception should escape.
    result = await middleware.on_call_tool(context, call_next)
    assert result.structured_content == {"status": "ok"}


async def test_orphan_mcp_session_resolves_to_none(fresh_telemetry):
    async def call_next(ctx):
        return _success_result({"status": "ok"})

    middleware = TelemetryMiddleware()
    context = _make_context(
        tool_name="list_files",
        arguments={},
        fastmcp_context=_FakeContext(session_id="never-bound-session"),
    )

    await middleware.on_call_tool(context, call_next)

    started = [event for event in runtime_telemetry._events if event.event_type == "tool.call.started"]
    # record_event drops None values from the payload; absence implies no resolved key.
    assert "runtime_session_key" not in started[0].data
