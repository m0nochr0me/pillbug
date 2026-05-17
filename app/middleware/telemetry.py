"""FastMCP middleware that audits every MCP tool invocation (plan P2 #16).

Emits two `runtime_telemetry` events per call:
  * `tool.call.started` with `tool_name`, `runtime_session_key`, `args_hash`,
    `args_summary` (truncated + secret-redacted) so replays can correlate to the args
    without storing them in full;
  * `tool.call.completed` with `duration_ms`, `result_status` (`"ok"`, `"error"`, or
    `"exception"`), and `error_type` when the tool raised.

The middleware never raises on its own: telemetry recording is best-effort and any
failure is swallowed so audit gaps cannot break tool execution.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.middleware import CallNext
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolRequestParams

from app.core.log import logger
from app.core.telemetry import runtime_telemetry
from app.runtime.session_binding import get_runtime_session_for_mcp_session
from app.util.text import redact_secrets

_ARGS_SUMMARY_MAX_CHARS = 200
_EVENT_PAYLOAD_SOFT_CAP_BYTES = 4096


def _args_payload(arguments: dict[str, Any] | None) -> tuple[str, str]:
    """Return (args_hash, args_summary) for telemetry."""
    args_json = json.dumps(arguments or {}, default=str, sort_keys=True)
    args_hash = hashlib.sha256(args_json.encode("utf-8")).hexdigest()
    redacted = redact_secrets(args_json)
    if len(redacted) > _ARGS_SUMMARY_MAX_CHARS:
        redacted = redacted[:_ARGS_SUMMARY_MAX_CHARS] + "…"
    return args_hash, redacted


def _resolve_runtime_session_key(context: MiddlewareContext[Any]) -> str | None:
    if context.fastmcp_context is None:
        return None
    try:
        mcp_session_id = context.fastmcp_context.session_id
    except Exception:  # session_id raises when no request context is active
        return None
    return get_runtime_session_for_mcp_session(mcp_session_id)


def _result_status(result: ToolResult) -> tuple[str, str | None]:
    """Extract (result_status, result_error_type) from a ToolResult envelope."""
    structured = result.structured_content
    if isinstance(structured, dict):
        status_value = structured.get("status")
        if isinstance(status_value, str):
            error_type = structured.get("type") if status_value == "error" else None
            return status_value, error_type if isinstance(error_type, str) else None
    return "ok", None


async def _safe_record_event(**kwargs: Any) -> None:
    try:
        await runtime_telemetry.record_event(**kwargs)
    except Exception:
        logger.exception("TelemetryMiddleware: failed to record audit event")


class TelemetryMiddleware(Middleware):
    """Emit tool.call.started and tool.call.completed events for every tool call."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        tool_name = context.message.name
        args_hash, args_summary = _args_payload(context.message.arguments)
        runtime_session_key = _resolve_runtime_session_key(context)

        started_data: dict[str, Any] = {
            "tool_name": tool_name,
            "runtime_session_key": runtime_session_key,
            "args_hash": args_hash,
            "args_summary": args_summary,
        }
        if len(json.dumps(started_data, default=str)) > _EVENT_PAYLOAD_SOFT_CAP_BYTES:
            started_data.pop("args_summary", None)
        await _safe_record_event(
            event_type="tool.call.started",
            source="mcp",
            level="info",
            message=f"tool call started: {tool_name}",
            data=started_data,
        )

        started_at = time.monotonic()
        result_status = "exception"
        error_type: str | None = None
        try:
            result = await call_next(context)
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        else:
            result_status, error_type = _result_status(result)
            return result
        finally:
            duration_ms = round((time.monotonic() - started_at) * 1000, 3)
            completed_data: dict[str, Any] = {
                "tool_name": tool_name,
                "runtime_session_key": runtime_session_key,
                "args_hash": args_hash,
                "duration_ms": duration_ms,
                "result_status": result_status,
            }
            if error_type is not None:
                completed_data["error_type"] = error_type
            await _safe_record_event(
                event_type="tool.call.completed",
                source="mcp",
                level="error" if result_status in {"exception", "error"} else "info",
                message=f"tool call completed: {tool_name} status={result_status}",
                data=completed_data,
            )
