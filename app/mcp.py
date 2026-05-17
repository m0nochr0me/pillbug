"""
Composition MCP Server
"""

import asyncio
import fnmatch
import hashlib
import json
import mimetypes
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastmcp import Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.middleware.logging import LoggingMiddleware

from app import __project__, __version__
from app.core.agent_card import build_extended_agent_card, build_public_agent_card
from app.core.config import settings
from app.core.log import logger, uvicorn_log_config
from app.core.telemetry import runtime_telemetry
from app.core.url_shortener import local_url_shortener
from app.middleware.compactor import CompactorMiddleware
from app.middleware.telemetry import TelemetryMiddleware
from app.runtime.approvals import approval_store, outbound_draft_store
from app.runtime.channels import describe_channel_telemetry, get_channel_plugin, register_channel_conversation
from app.runtime.mcp_plugins import load_mcp_tool_plugins
from app.runtime.outbound_budget import DEFAULT_NON_CLI_LIMITS, outbound_send_budget
from app.runtime.scheduler import task_scheduler
from app.runtime.session_binding import (
    bind_runtime_session_todo_snapshot,
    get_runtime_session_for_mcp_session,
    get_runtime_session_origin_metadata,
    record_pending_outbound_injection,
    record_runtime_session_skill_load,
    split_runtime_session_key,
)
from app.runtime.session_mode import (
    SessionMode,
    get_planning_state,
    get_session_mode,
    planning_block_reminder,
)
from app.runtime.session_mode import (
    enter_planning_mode as _registry_enter_planning,
)
from app.runtime.session_mode import (
    exit_planning_mode as _registry_exit_planning,
)
from app.runtime.task_runtime_state import task_forbidden_actions_for_session
from app.schema.control import (
    ApprovalDecision,
    ApprovedAction,
    AuthScope,
    AuthTokenBinding,
    ControlMessageRequest,
    OperatorResponse,
    OutboundAttachmentDraft,
    OutboundDraft,
    OutboundDraftDecision,
    OutboundDraftKind,
    PlanningModeRequest,
    RuntimeAuthConfiguration,
)
from app.schema.messages import (
    A2AEnvelope,
    A2ATarget,
    OutboundAttachment,
    build_a2a_origin_routing_metadata,
    extract_a2a_origin_channel_metadata,
    extract_a2a_origin_route,
)
from app.schema.tasks import AgentTaskGoal
from app.schema.telemetry import ChannelsTelemetrySnapshot, RuntimeMetadata
from app.schema.todo import TodoItem, TodoListSnapshot
from app.util.skills import workspace_skill_name_for_path
from app.util.text import classify_shell_stderr
from app.util.tool_result import envelope_error, tool_error
from app.util.web import (
    build_fetch_output_path,
    decode_text_payload,
    extract_readable_html,
    looks_like_html,
    looks_like_text,
    parse_trust_banner,
    render_readable_html_document,
    render_trust_banner,
    render_trust_banner_metadata,
)
from app.util.workspace import (
    async_read_text_file,
    async_write_bytes_file,
    async_write_text_file,
    display_path,
    is_hidden_path,
    resolve_path_within_root,
    truncate_text,
)

__all__ = ("create_mcp_server", "mcp", "mcp_app")

mcp = FastMCP(f"{__project__}-composition-server")
mcp.add_middleware(LoggingMiddleware(include_payloads=True, max_payload_length=1000))
mcp.add_middleware(TelemetryMiddleware())

_TODO_LIST_STATE_KEY = "todo_list"


def _display_path(path: Path) -> str:
    return display_path(path, settings.WORKSPACE_ROOT)


def _resolve_workspace_path(path: str | Path) -> Path:
    return resolve_path_within_root(path, settings.WORKSPACE_ROOT)


def _validate_page_size(page_size: int) -> int:
    if page_size < 1:
        raise ValueError("page_size must be at least 1")

    return min(page_size, settings.MCP_MAX_PAGE_SIZE)


def _validate_max_results(max_results: int) -> int:
    if max_results < 1:
        raise ValueError("max_results must be at least 1")

    return min(max_results, settings.MCP_MAX_SEARCH_RESULTS)


def _validate_command_timeout(timeout_seconds: float) -> float:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0")

    return min(timeout_seconds, settings.MCP_MAX_COMMAND_TIMEOUT_SECONDS)


def _validate_fetch_url_max_bytes(max_bytes: int) -> int:
    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")

    return min(max_bytes, settings.MCP_FETCH_URL_MAX_BYTES)


def _get_command_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell and Path(shell).is_file():
        return shell

    return "/bin/sh"


def _parse_channel_target(channel: str) -> tuple[str, str]:
    channel_name, separator, conversation_id = channel.strip().partition(":")
    if not channel_name:
        raise ValueError("channel must not be empty")

    if not separator:
        return channel_name, ""

    if not conversation_id:
        raise ValueError("channel targets using ':' must include a destination after the channel name")

    return channel_name, conversation_id


def _resolve_a2a_origin_routing_metadata(
    runtime_session_key: str,
    session_origin_metadata: dict[str, object] | None,
) -> dict[str, object] | None:
    if session_origin_metadata is not None and (
        existing_origin_route := extract_a2a_origin_route(session_origin_metadata)
    ):
        return build_a2a_origin_routing_metadata(
            channel_name=existing_origin_route[0],
            conversation_id=existing_origin_route[1],
            channel_metadata=extract_a2a_origin_channel_metadata(session_origin_metadata),
        )

    origin_route = split_runtime_session_key(runtime_session_key)
    if origin_route is None:
        return None

    return build_a2a_origin_routing_metadata(
        channel_name=origin_route[0],
        conversation_id=origin_route[1],
        channel_metadata=session_origin_metadata,
    )


def _get_outbound_a2a_metadata(ctx: Context | None) -> dict[str, object] | None:
    if ctx is None:
        return None

    runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
    if runtime_session_key is None:
        return None

    origin_channel_metadata = get_runtime_session_origin_metadata(runtime_session_key)
    resolved_origin_metadata = _resolve_a2a_origin_routing_metadata(runtime_session_key, origin_channel_metadata)
    if resolved_origin_metadata is None:
        return None

    return dict(resolved_origin_metadata)


def _track_outbound_conversation(channel_name: str, conversation_id: str) -> None:
    if not conversation_id:
        return

    register_channel_conversation(channel_name, conversation_id)
    application_loop = mcp_app.state.application_loop
    if application_loop is not None:
        application_loop.track_outbound_conversation(channel_name, conversation_id)


def _get_a2a_channel_plugin(create: bool = True) -> Any:
    channel_plugin = get_channel_plugin("a2a", create=create)
    if channel_plugin is None:
        raise ValueError("A2A channel is not enabled or available")

    return channel_plugin


def _normalize_a2a_target(target: str) -> str:
    return A2ATarget.parse(target).as_conversation_target()


def _assert_not_echoing_a2a_origin(
    *,
    channel_name: str,
    conversation_id: str,
    ctx: Context | None,
) -> None:
    if ctx is None:
        return

    runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
    if runtime_session_key is None:
        return

    session_route = split_runtime_session_key(runtime_session_key)
    if session_route is None or session_route[0] != "a2a":
        return

    session_origin_metadata = get_runtime_session_origin_metadata(runtime_session_key)
    if session_origin_metadata is None:
        return

    origin_route = extract_a2a_origin_route(session_origin_metadata)
    if origin_route is None:
        return

    if (channel_name, conversation_id) != origin_route:
        return

    raise ValueError(
        "This local A2A session already knows how to reach the preserved origin channel. "
        "Reply normally in the current session instead of using send_message to echo the result manually."
    )


async def _get_todo_snapshot(ctx: Context) -> TodoListSnapshot:
    state = await ctx.get_state(_TODO_LIST_STATE_KEY)
    if state is None:
        _sync_todo_snapshot_to_runtime_session(ctx, None)
        return TodoListSnapshot()

    snapshot = TodoListSnapshot.model_validate(state)
    _sync_todo_snapshot_to_runtime_session(ctx, snapshot)
    return snapshot


def _sync_todo_snapshot_to_runtime_session(ctx: Context | None, snapshot: TodoListSnapshot | None) -> None:
    if ctx is None:
        return

    runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
    if runtime_session_key is None:
        return

    bind_runtime_session_todo_snapshot(runtime_session_key, snapshot)


def _serialize_todo_snapshot(action: str, snapshot: TodoListSnapshot) -> dict[str, Any]:
    return {
        "action": action,
        "items": [item.model_dump(mode="json") for item in snapshot.items],
        "total": len(snapshot.items),
        "counts": snapshot.counts,
        "explanation": snapshot.explanation,
        "updated_at": snapshot.updated_at.isoformat(),
    }


def _build_runtime_metadata() -> RuntimeMetadata:
    return runtime_telemetry.metadata()


def _build_runtime_auth_configuration() -> RuntimeAuthConfiguration:
    token_bindings: list[AuthTokenBinding] = []
    dashboard_token = settings.dashboard_bearer_token()
    a2a_token = settings.a2a_bearer_token()

    if dashboard_token is not None:
        token_bindings.append(
            AuthTokenBinding(
                token_name="dashboard-bearer",
                principal="dashboard",
                scopes=(AuthScope.TELEMETRY, AuthScope.CONTROL),
            )
        )

    if a2a_token is not None:
        token_bindings.append(
            AuthTokenBinding(
                token_name="a2a-bearer",
                principal="a2a",
                scopes=(AuthScope.A2A,),
            )
        )

    return RuntimeAuthConfiguration(
        token_bindings=tuple(token_bindings),
        telemetry_protected=dashboard_token is not None,
        control_protected=True,
        a2a_protected=a2a_token is not None,
    )


def _extract_bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None

    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    return token.strip() or None


async def _authorize_telemetry(authorization: str | None) -> AuthScope | None:
    expected_token = settings.dashboard_bearer_token()
    if expected_token is None:
        return None

    presented_token = _extract_bearer_token(authorization)
    if presented_token is None or not secrets.compare_digest(presented_token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthScope.TELEMETRY


async def _authorize_control(authorization: str | None) -> AuthScope:
    expected_token = settings.dashboard_bearer_token()
    if expected_token is None:
        raise HTTPException(
            status_code=503,
            detail="Control API requires PB_DASHBOARD_BEARER_TOKEN to be configured.",
        )

    presented_token = _extract_bearer_token(authorization)
    if presented_token is None or not secrets.compare_digest(presented_token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthScope.CONTROL


async def _authorize_a2a(authorization: str | None) -> AuthScope:
    expected_token = settings.a2a_bearer_token()
    if expected_token is None:
        return AuthScope.A2A

    presented_token = _extract_bearer_token(authorization)
    if presented_token is None or not secrets.compare_digest(presented_token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthScope.A2A


def _ensure_a2a_discovery_available() -> None:
    if "a2a" not in settings.enabled_channels():
        raise HTTPException(status_code=404, detail="A2A discovery is not enabled on this runtime.")


def _agent_card_response(request: Request, payload: dict[str, Any], *, cache_control: str) -> JSONResponse:
    response_body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    etag = hashlib.sha256(response_body).hexdigest()

    if request.headers.get("if-none-match") == etag:
        return JSONResponse(
            status_code=304,
            content=None,
            headers={
                "Cache-Control": cache_control,
                "ETag": etag,
            },
            media_type="application/a2a+json",
        )

    return JSONResponse(
        content=payload,
        media_type="application/a2a+json",
        headers={
            "Cache-Control": cache_control,
            "ETag": etag,
        },
    )


def _operator_response(
    *,
    action: str,
    message: str,
    scope: AuthScope,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return OperatorResponse(
        runtime_id=settings.runtime_id,
        ok=True,
        action=action,
        message=message,
        scope=scope,
        details=details,
    ).model_dump(mode="json")


async def _audit_control_action(
    request: Request,
    *,
    action: str,
    scope: AuthScope,
    message: str,
    level: Literal["info", "warning", "error"] = "info",
    details: dict[str, Any] | None = None,
) -> None:
    payload = {
        "runtime_id": settings.runtime_id,
        "scope": scope.value,
        "action": action,
        "path": str(request.url.path),
        "client_host": request.client.host if request.client is not None else None,
        **{key: value for key, value in (details or {}).items() if value is not None},
    }
    rendered_payload = json.dumps(payload, sort_keys=True, default=str)

    if level == "error":
        logger.error(f"Control action {message} {rendered_payload}")
    elif level == "warning":
        logger.warning(f"Control action {message} {rendered_payload}")
    else:
        logger.info(f"Control action {message} {rendered_payload}")

    await runtime_telemetry.record_event(
        event_type=f"control.{action}",
        source="control-api",
        level=level,
        message=message,
        data=payload,
    )


def _format_sse_event(
    event_payload: dict[str, Any], *, event_name: str = "message", event_id: str | None = None
) -> str:
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_name}")
    lines.append(f"data: {json.dumps(event_payload, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


@mcp.tool
def get_runtime_info() -> dict[str, Any]:
    """
    Provides a runtime info
    """
    return _build_runtime_metadata().model_dump(mode="json")


@mcp.tool
@envelope_error
async def list_files(
    directory: str = ".",
    include_hidden: bool = False,
) -> dict[str, Any]:
    """
    Lists files and directories directly under a workspace-relative directory.
    """

    target_directory = _resolve_workspace_path(directory)

    if not await asyncio.to_thread(target_directory.exists):
        return tool_error("not_found", f"Directory does not exist: {directory}")

    if not await asyncio.to_thread(target_directory.is_dir):
        return tool_error("invalid_arguments", f"Path is not a directory: {directory}")

    def build_entries() -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []

        for entry in sorted(target_directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            relative_path = entry.relative_to(settings.WORKSPACE_ROOT)
            if not include_hidden and is_hidden_path(relative_path):
                continue

            entry_type = "directory" if entry.is_dir() else "file"
            entries.append(
                {
                    "name": entry.name,
                    "path": _display_path(entry),
                    "type": entry_type,
                    "size": entry.stat().st_size if entry.is_file() else None,
                },
            )

        return entries

    entries = await asyncio.to_thread(build_entries)
    logger.debug(f"Listed {len(entries)} entries in {_display_path(target_directory)}")

    return {
        "directory": _display_path(target_directory),
        "entries": entries,
        "count": len(entries),
    }


@mcp.tool
@envelope_error
async def read_file(
    path: str,
    start_line: int = 1,
    page_size: int = settings.MCP_DEFAULT_PAGE_SIZE,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Reads a UTF-8 text file from the workspace with line-based pagination.
    """

    if start_line < 1:
        return tool_error("invalid_arguments", "start_line must be at least 1")

    page_size = _validate_page_size(page_size)
    target_file = _resolve_workspace_path(path)

    if not await asyncio.to_thread(target_file.exists):
        return tool_error(
            "not_found",
            f"File does not exist: {path}",
            next_valid_actions=("find_files", "list_files"),
        )

    if not await asyncio.to_thread(target_file.is_file):
        return tool_error("invalid_arguments", f"Path is not a file: {path}")

    content = await async_read_text_file(target_file)
    provenance = parse_trust_banner(content)
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    start_index = min(start_line - 1, total_lines)
    page_lines = lines[start_index : start_index + page_size]
    end_line = start_index + len(page_lines)

    logger.debug(f"Read {len(page_lines)} lines from {_display_path(target_file)} starting at line {start_line}")

    # P1 #9 hook: when the model reads a SKILL.md, record the load so the rehydration
    # bundle can remind it which skills are already in context after a compress.
    # P2 #18: emit a one-shot `skill.loaded` telemetry event so operators can see hot skills.
    if ctx is not None:
        skill_name = workspace_skill_name_for_path(target_file)
        if skill_name is not None:
            runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
            if runtime_session_key:
                newly_loaded = record_runtime_session_skill_load(runtime_session_key, skill_name)
                if newly_loaded:
                    await runtime_telemetry.record_event(
                        event_type="skill.loaded",
                        source="mcp",
                        level="info",
                        message=f"skill loaded: {skill_name}",
                        data={
                            "skill_name": skill_name,
                            "runtime_session_key": runtime_session_key,
                        },
                    )

    result: dict[str, Any] = {
        "path": _display_path(target_file),
        "start_line": start_line,
        "end_line": end_line,
        "page_size": page_size,
        "total_lines": total_lines,
        "has_more": end_line < total_lines,
        "content": "".join(page_lines),
    }
    if provenance is not None:
        result["provenance"] = provenance[0]
    return result


@mcp.tool
@envelope_error
async def write_new_file(
    path: str,
    content: str,
    make_parents: bool = True,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Creates a new UTF-8 text file in the workspace and fails if it already exists.
    """

    if blocked := _enforce_planning_gate("write_new_file", ctx):
        return blocked

    target_file = _resolve_workspace_path(path)

    if await asyncio.to_thread(target_file.exists):
        return tool_error(
            "conflict",
            f"File already exists: {path}",
            next_valid_actions=("replace_file_text", "read_file"),
        )

    if make_parents:
        await asyncio.to_thread(target_file.parent.mkdir, parents=True, exist_ok=True)
    elif not await asyncio.to_thread(target_file.parent.exists):
        return tool_error(
            "not_found",
            f"Parent directory does not exist: {_display_path(target_file.parent)}",
        )

    chars_written = await async_write_text_file(target_file, content, mode="x")
    logger.info(f"Created file {_display_path(target_file)}")

    return {
        "path": _display_path(target_file),
        "chars_written": chars_written,
    }


@mcp.tool
@envelope_error
async def replace_file_text(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    expected_occurrences: int | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Replaces literal text inside an existing UTF-8 text file.
    """

    if blocked := _enforce_planning_gate("replace_file_text", ctx):
        return blocked

    if not old_text:
        return tool_error("invalid_arguments", "old_text must not be empty")

    target_file = _resolve_workspace_path(path)

    if not await asyncio.to_thread(target_file.exists):
        return tool_error("not_found", f"File does not exist: {path}")

    if not await asyncio.to_thread(target_file.is_file):
        return tool_error("invalid_arguments", f"Path is not a file: {path}")

    content = await async_read_text_file(target_file)
    occurrences = content.count(old_text)

    if occurrences == 0:
        return tool_error(
            "not_found",
            "old_text was not found in the file",
            next_valid_actions=("search_file_regex", "read_file"),
        )

    if expected_occurrences is not None and occurrences != expected_occurrences:
        return tool_error(
            "conflict",
            f"Expected {expected_occurrences} occurrences of old_text, but found {occurrences}",
            details={"occurrences_found": occurrences, "expected": expected_occurrences},
        )

    replacement_count = occurrences if replace_all else 1
    updated_content = content.replace(old_text, new_text, replacement_count)
    await async_write_text_file(target_file, updated_content, mode="w")
    logger.info(f"Replaced {replacement_count} occurrence(s) in {_display_path(target_file)}")

    return {
        "path": _display_path(target_file),
        "occurrences_found": occurrences,
        "occurrences_replaced": replacement_count,
    }


@mcp.tool
@envelope_error
async def search_file_regex(
    path: str,
    pattern: str,
    max_results: int = 50,
) -> dict[str, Any]:
    """
    Searches a UTF-8 text file line by line using a regular expression.
    """

    target_file = _resolve_workspace_path(path)
    max_results = _validate_max_results(max_results)

    if not await asyncio.to_thread(target_file.exists):
        return tool_error("not_found", f"File does not exist: {path}")

    if not await asyncio.to_thread(target_file.is_file):
        return tool_error("invalid_arguments", f"Path is not a file: {path}")

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return tool_error("invalid_arguments", f"Invalid regular expression: {exc}")

    content = await async_read_text_file(target_file)
    matches: list[dict[str, Any]] = []
    truncated = False

    for line_number, line in enumerate(content.splitlines(), start=1):
        for match in regex.finditer(line):
            matches.append(
                {
                    "line": line_number,
                    "start_column": match.start() + 1,
                    "end_column": match.end(),
                    "match": match.group(0),
                    "line_text": line,
                },
            )

            if len(matches) >= max_results:
                truncated = True
                break

        if truncated:
            break

    logger.debug(f"Found {len(matches)} regex matches in {_display_path(target_file)}")

    return {
        "path": _display_path(target_file),
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
        "truncated": truncated,
    }


@mcp.tool
@envelope_error
async def find_files(
    pattern: str,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """
    Finds workspace files by glob pattern relative to the workspace root.
    """

    def run_glob() -> list[str]:
        matches: list[str] = []

        for candidate in sorted(settings.WORKSPACE_ROOT.glob(pattern)):
            if not candidate.is_file():
                continue

            relative_path = candidate.relative_to(settings.WORKSPACE_ROOT)
            if not include_hidden and is_hidden_path(relative_path):
                continue

            matches.append(str(relative_path))

        return matches

    matches = await asyncio.to_thread(run_glob)
    logger.debug(f"Glob pattern {pattern} matched {len(matches)} files")

    return {
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
    }


def _approvals_bypassed() -> bool:
    """P1 #22: when true, approval gates are short-circuited (yolo mode)."""
    return settings.DANGEROUSLY_APPROVE_EVERYTHING


def _channel_is_autosend(channel_name: str) -> bool:
    if _approvals_bypassed():
        return True
    return channel_name in settings.outbound_autosend_channels()


def _outbound_source_label(ctx: Context | None) -> str:
    if ctx is not None:
        runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
        if runtime_session_key:
            return runtime_session_key
    return "mcp"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _resolve_runtime_session_key(ctx: Context | None) -> str | None:
    if ctx is None:
        return None
    return get_runtime_session_for_mcp_session(ctx.session_id)


def _build_agent_task_goal(
    *,
    done_condition: str | None,
    validation_prompt: str | None,
    max_steps_per_run: int | None,
    max_cost_per_run_usd: float | None,
    forbidden_actions: list[str] | None,
    progress_log_path: str | None,
) -> AgentTaskGoal | None:
    """Construct an AgentTaskGoal from the manage_agent_task kwargs, or None when all empty."""
    has_any_field = any(
        value is not None and value != []
        for value in (
            done_condition,
            validation_prompt,
            max_steps_per_run,
            max_cost_per_run_usd,
            forbidden_actions,
            progress_log_path,
        )
    )
    if not has_any_field:
        return None
    return AgentTaskGoal(
        done_condition=done_condition,
        validation_prompt=validation_prompt,
        max_steps_per_run=max_steps_per_run,
        max_cost_per_run_usd=max_cost_per_run_usd,
        forbidden_actions=tuple(forbidden_actions or ()),
        progress_log_path=progress_log_path,
    )


_PLANNING_READ_ONLY_TOOLS: tuple[str, ...] = (
    "list_files",
    "read_file",
    "find_files",
    "search_file_regex",
    "fetch_url",
    "exit_planning_mode",
)


def _enforce_planning_gate(tool_name: str, ctx: Context | None) -> dict[str, Any] | None:
    """P2 #11: deny mutating tools while the session is in planning mode.

    Also enforces per-task `goal.forbidden_actions` (plan P2 #12) for scheduled-task
    sessions; both checks share this gate so the call sites stay symmetric.
    """
    runtime_session_key = _resolve_runtime_session_key(ctx)
    if runtime_session_key is None:
        return None
    if get_session_mode(runtime_session_key) is SessionMode.PLANNING:
        return tool_error(
            "denied",
            planning_block_reminder(),
            next_valid_actions=_PLANNING_READ_ONLY_TOOLS,
            details={
                "tool": tool_name,
                "session_key": runtime_session_key,
                "reason": "planning_mode_blocked",
            },
        )
    forbidden = task_forbidden_actions_for_session(runtime_session_key)
    if forbidden and _tool_matches_forbidden_action(tool_name, forbidden):
        return tool_error(
            "denied",
            f"Tool {tool_name!r} is in this task's forbidden_actions list.",
            details={
                "tool": tool_name,
                "session_key": runtime_session_key,
                "reason": "task_forbidden_action",
                "forbidden_actions": sorted(forbidden),
            },
        )
    return None


def _tool_matches_forbidden_action(tool_name: str, forbidden: frozenset[str]) -> bool:
    """Match a tool name against the forbidden_actions set.

    A bare entry like `manage_agent_task` blocks every sub-action; the tool itself
    passes its action-suffixed name (e.g. `manage_agent_task.create`) so we match on
    both the full name and on the part before any `.` boundary.
    """
    if tool_name in forbidden:
        return True
    prefix = tool_name.partition(".")[0]
    return prefix in forbidden


_PLANNING_ARTIFACT_DIRECTORY = "plans/active"


def _planning_artifact_path(session_key: str, timestamp: datetime) -> Path:
    safe_session_key = session_key.replace(":", "__").replace("/", "_") or "session"
    timestamp_label = timestamp.strftime("%Y%m%dT%H%M%SZ")
    return _resolve_workspace_path(f"{_PLANNING_ARTIFACT_DIRECTORY}/{safe_session_key}-{timestamp_label}.md")


def _render_planning_artifact(
    *,
    session_key: str,
    objective: str,
    scope: str | None,
    plan_summary: str,
    entered_at: datetime,
    exited_at: datetime,
    source: str,
) -> str:
    front_matter_lines = [
        "---",
        f"session_key: {session_key}",
        f"source: {source}",
        f"entered_at: {entered_at.isoformat()}",
        f"exited_at: {exited_at.isoformat()}",
        f"objective: {objective}",
    ]
    if scope is not None:
        front_matter_lines.append(f"scope: {scope}")
    front_matter_lines.append("---")
    front_matter_lines.append("")
    return "\n".join(front_matter_lines) + "\n" + plan_summary.rstrip() + "\n"


async def _write_planning_artifact(
    *,
    session_key: str,
    objective: str,
    scope: str | None,
    plan_summary: str,
    entered_at: datetime,
    exited_at: datetime,
    source: str,
) -> Path:
    target_file = _planning_artifact_path(session_key, exited_at)
    await asyncio.to_thread(target_file.parent.mkdir, parents=True, exist_ok=True)
    content = _render_planning_artifact(
        session_key=session_key,
        objective=objective,
        scope=scope,
        plan_summary=plan_summary,
        entered_at=entered_at,
        exited_at=exited_at,
        source=source,
    )
    await async_write_text_file(target_file, content, mode="w")
    return target_file


def _requires_approval_envelope(record: OutboundDraft) -> dict[str, Any]:
    return {
        "status": "requires_approval",
        "draft_id": record.id,
        "kind": record.kind.value,
        "channel": record.channel,
        "target": record.target,
        "next_valid_actions": ["wait_for_operator_commit"],
        "message": (
            f"Outbound draft recorded; operator must commit via "
            f"POST /control/drafts/{record.id}/commit before this send takes effect."
        ),
    }


async def _dispatch_send_message(record: OutboundDraft, *, ctx: Context | None) -> dict[str, Any]:
    channel_plugin = get_channel_plugin(record.channel, create=True)
    if channel_plugin is None:
        return tool_error("not_found", f"Channel is not enabled or available: {record.channel}")

    conversation_id = record.target
    await channel_plugin.send_message(conversation_id or "", record.message, metadata=None)
    _track_outbound_conversation(record.channel, conversation_id)

    if settings.SESSION_CONTINUITY and ctx is not None:
        source_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
        target_session_key = f"{record.channel}:{conversation_id}" if conversation_id else record.channel
        if source_session_key and source_session_key != target_session_key:
            record_pending_outbound_injection(source_session_key, target_session_key)

    logger.info(f"Sent outbound message via channel={record.channel} destination={conversation_id or '<default>'}")
    return {
        "channel": record.channel,
        "conversation_id": conversation_id or None,
        "chars_sent": len(record.message),
    }


async def _dispatch_send_file(record: OutboundDraft) -> dict[str, Any]:
    if record.attachment is None:
        return tool_error("invalid_arguments", "send_file draft is missing the attachment payload")

    channel_plugin = get_channel_plugin(record.channel, create=True)
    if channel_plugin is None:
        return tool_error("not_found", f"Channel is not enabled or available: {record.channel}")

    conversation_id = record.target
    resolved_path = _resolve_workspace_path(record.attachment.path)
    if not resolved_path.is_file():
        return tool_error("not_found", f"File not found in workspace: {_display_path(resolved_path)}")

    mime_type = mimetypes.guess_type(resolved_path.name)[0]
    attachment = OutboundAttachment(
        path=str(resolved_path),
        mime_type=mime_type,
        display_name=record.attachment.caption,
        send_as=record.attachment.send_as,
    )

    await channel_plugin.send_message(
        conversation_id or "",
        record.attachment.caption or record.message or "",
        attachments=(attachment,),
    )

    logger.info(
        f"Sent file via channel={record.channel} destination={conversation_id or '<default>'} path={_display_path(resolved_path)}"
    )
    return {
        "channel": record.channel,
        "conversation_id": conversation_id or None,
        "file": _display_path(resolved_path),
        "send_as": record.attachment.send_as or "auto",
    }


async def _dispatch_send_a2a_message(record: OutboundDraft, *, ctx: Context | None) -> dict[str, Any]:
    channel_plugin = _get_a2a_channel_plugin(create=True)
    metadata = _get_outbound_a2a_metadata(ctx)
    await channel_plugin.send_message(record.target, record.message, metadata=metadata)
    _track_outbound_conversation("a2a", record.target)
    return {
        "channel": "a2a",
        "mode": "async",
        "target": record.target,
        "chars_sent": len(record.message),
    }


async def _dispatch_request_a2a_response(record: OutboundDraft, *, ctx: Context | None) -> dict[str, Any]:
    channel_plugin = _get_a2a_channel_plugin(create=True)
    send_request = getattr(channel_plugin, "send_request", None)
    if not callable(send_request):
        return tool_error(
            "permission_denied",
            "Configured A2A channel does not support synchronous requests",
        )

    send_request_callable = cast("Callable[..., Awaitable[A2AEnvelope]]", send_request)
    metadata = _get_outbound_a2a_metadata(ctx)
    response_envelope = await send_request_callable(
        record.target,
        record.message,
        metadata=metadata,
        timeout_seconds=record.timeout_seconds if record.timeout_seconds is not None else 60.0,
    )
    return {
        "channel": "a2a",
        "mode": "sync",
        "target": record.target,
        "sender_runtime_id": response_envelope.sender_runtime_id,
        "conversation_id": response_envelope.conversation_id,
        "intent": response_envelope.intent.value,
        "response_text": response_envelope.text,
        "message_id": response_envelope.message_id,
        "reply_to_message_id": response_envelope.reply_to_message_id,
        "hop_count": response_envelope.convergence_state.hop_count,
        "max_hops": response_envelope.convergence_state.max_hops,
        "stop_requested": response_envelope.convergence_state.stop_requested,
    }


def _outbound_limits_for_channel(channel: str) -> dict[str, int] | None:
    """Resolve the rolling-window budget for `channel`. None = unlimited (plan P2 #15)."""
    configured = settings.outbound_send_limits()
    if channel in configured:
        return configured[channel] or None
    if channel == "cli":
        return None
    return dict(DEFAULT_NON_CLI_LIMITS)


def _check_outbound_budget(channel: str, conversation_id: str) -> dict[str, Any] | None:
    limits = _outbound_limits_for_channel(channel)
    if limits is None:
        return None
    reason = outbound_send_budget.check_and_charge(channel, conversation_id, limits)
    if reason is None:
        return None
    return tool_error(
        "rate_limited",
        f"Outbound send budget exceeded for channel {channel!r} ({reason})",
        next_valid_actions=("wait", "request_approval"),
        details={
            "channel": channel,
            "conversation_id": conversation_id,
            "reason": reason,
            "limits": dict(limits),
        },
    )


async def _dispatch_outbound_draft(record: OutboundDraft, *, ctx: Context | None) -> dict[str, Any]:
    if blocked := _check_outbound_budget(record.channel, record.target):
        await runtime_telemetry.record_event(
            event_type="outbound.rate_limited",
            source="mcp",
            level="warning",
            message=f"rate_limited: {record.channel}",
            data={
                "draft_id": record.id,
                "channel": record.channel,
                "target": record.target,
                "reason": blocked["details"]["reason"],
            },
        )
        return blocked
    if record.kind == OutboundDraftKind.SEND_MESSAGE:
        return await _dispatch_send_message(record, ctx=ctx)
    if record.kind == OutboundDraftKind.SEND_FILE:
        return await _dispatch_send_file(record)
    if record.kind == OutboundDraftKind.SEND_A2A_MESSAGE:
        return await _dispatch_send_a2a_message(record, ctx=ctx)
    if record.kind == OutboundDraftKind.REQUEST_A2A_RESPONSE:
        return await _dispatch_request_a2a_response(record, ctx=ctx)
    return tool_error("internal_error", f"Unknown outbound draft kind: {record.kind}")


async def _emit_outbound_draft_event(record: OutboundDraft) -> None:
    await runtime_telemetry.record_event(
        event_type="control.draft_created",
        source="mcp",
        level="info",
        message="drafted",
        data={
            "draft_id": record.id,
            "kind": record.kind.value,
            "channel": record.channel,
            "target": record.target,
            "source": record.source,
        },
    )


@mcp.tool
@envelope_error
async def send_message(
    channel: str,
    message: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Sends a direct outbound message to a configured channel.

    Use this when the agent needs to initiate or continue a message outside the normal reply path,
    such as proactive follow-ups from a scheduled task or notifying a non-A2A channel.
    Do not use this to answer the current inbound message on the same channel during the active turn;
    the application loop will send that response automatically.

    Do not use this tool for cross-runtime A2A communication.
    Use send_a2a_message for asynchronous A2A handoffs and request_a2a_response when you need a peer reply in the same turn.

    The channel argument accepts either a bare channel name for default destinations such as cli,
    or a session-style target in the form channel_name:conversation_id such as telegram:123456789.

    Channels in PB_OUTBOUND_AUTOSEND_CHANNELS (default 'cli') dispatch immediately; off-allowlist
    targets return a requires_approval envelope with a draft_id the operator must commit via
    POST /control/drafts/{draft_id}/commit.
    """

    if blocked := _enforce_planning_gate("send_message", ctx):
        return blocked

    if not message.strip():
        return tool_error("invalid_arguments", "message must not be empty")

    channel_name, conversation_id = _parse_channel_target(channel)
    _assert_not_echoing_a2a_origin(
        channel_name=channel_name,
        conversation_id=conversation_id,
        ctx=ctx,
    )
    channel_plugin = get_channel_plugin(channel_name, create=True)
    if channel_plugin is None:
        return tool_error("not_found", f"Channel is not enabled or available: {channel_name}")

    record = await outbound_draft_store.create(
        kind=OutboundDraftKind.SEND_MESSAGE,
        channel=channel_name,
        target=conversation_id or "",
        message=message,
        source=_outbound_source_label(ctx),
    )
    await _emit_outbound_draft_event(record)

    if _channel_is_autosend(channel_name):
        committed = await outbound_draft_store.commit(record.id, decided_by="autosend")
        return await _dispatch_outbound_draft(committed, ctx=ctx)

    return _requires_approval_envelope(record)


@mcp.tool
@envelope_error
async def send_file(
    channel: str,
    path: str,
    caption: str | None = None,
    send_as: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Sends a workspace file as an attachment to a configured channel.

    Use this to deliver files from the workspace to a user on a specific channel.
    The channel argument accepts the same format as send_message:
    a bare channel name like cli, or a session-style target like telegram:123456789.

    The path argument should be a workspace-relative or absolute path to the file.
    The optional send_as argument hints how the channel should deliver the file:
    voice (Telegram voice message, best with .ogg opus files),
    audio (music/audio player), photo, video, or document (default, any file type).
    If omitted, the channel infers the delivery method from the file MIME type.

    Targets outside PB_OUTBOUND_AUTOSEND_CHANNELS return requires_approval until the operator
    commits the draft via POST /control/drafts/{draft_id}/commit.
    """

    if blocked := _enforce_planning_gate("send_file", ctx):
        return blocked

    channel_name, conversation_id = _parse_channel_target(channel)
    channel_plugin = get_channel_plugin(channel_name, create=True)
    if channel_plugin is None:
        return tool_error("not_found", f"Channel is not enabled or available: {channel_name}")

    resolved_path = _resolve_workspace_path(path)
    if not resolved_path.is_file():
        return tool_error(
            "not_found",
            f"File not found in workspace: {_display_path(resolved_path)}",
        )

    record = await outbound_draft_store.create(
        kind=OutboundDraftKind.SEND_FILE,
        channel=channel_name,
        target=conversation_id or "",
        message=caption or "",
        source=_outbound_source_label(ctx),
        attachment=OutboundAttachmentDraft(path=str(resolved_path), caption=caption, send_as=send_as),
    )
    await _emit_outbound_draft_event(record)

    if _channel_is_autosend(channel_name):
        committed = await outbound_draft_store.commit(record.id, decided_by="autosend")
        return await _dispatch_outbound_draft(committed, ctx=ctx)

    return _requires_approval_envelope(record)


@mcp.tool
@envelope_error
async def send_a2a_message(
    target: str,
    message: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Sends an asynchronous A2A message to another runtime and returns immediately.

    Use this when another runtime should continue work in the background and you will handle the result later.
    The target may use runtime_id/conversation_id or a2a:runtime_id/conversation_id such as runtime-b/deploy-42
    or a2a:runtime-b/deploy-42.

    If you later receive a terminal A2A result in a local a2a session, answer normally in that session when you want
    to respond to the original requester. The runtime will route that final response back to the preserved origin channel.
    """

    if blocked := _enforce_planning_gate("send_a2a_message", ctx):
        return blocked

    if not message.strip():
        return tool_error("invalid_arguments", "message must not be empty")

    normalized_target = _normalize_a2a_target(target)
    _get_a2a_channel_plugin(create=True)  # validate channel availability up front

    record = await outbound_draft_store.create(
        kind=OutboundDraftKind.SEND_A2A_MESSAGE,
        channel="a2a",
        target=normalized_target,
        message=message,
        source=_outbound_source_label(ctx),
    )
    await _emit_outbound_draft_event(record)

    if _channel_is_autosend("a2a"):
        committed = await outbound_draft_store.commit(record.id, decided_by="autosend")
        return await _dispatch_outbound_draft(committed, ctx=ctx)

    return _requires_approval_envelope(record)


@mcp.tool
@envelope_error
async def request_a2a_response(
    target: str,
    message: str,
    timeout_seconds: float = 60.0,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Sends a synchronous A2A request to another runtime and waits for its terminal reply.

    Use this when you need the peer runtime's answer before you continue the current turn.
    The target may use runtime_id/conversation_id or a2a:runtime_id/conversation_id such as runtime-b/deploy-42
    or a2a:runtime-b/deploy-42.
    The returned response_text can be used directly in your final answer or incorporated into a larger response.
    """

    if blocked := _enforce_planning_gate("request_a2a_response", ctx):
        return blocked

    if not message.strip():
        return tool_error("invalid_arguments", "message must not be empty")

    normalized_target = _normalize_a2a_target(target)
    timeout_seconds = _validate_command_timeout(timeout_seconds)
    channel_plugin = _get_a2a_channel_plugin(create=True)
    send_request = getattr(channel_plugin, "send_request", None)
    if not callable(send_request):
        return tool_error(
            "permission_denied",
            "Configured A2A channel does not support synchronous requests",
        )

    record = await outbound_draft_store.create(
        kind=OutboundDraftKind.REQUEST_A2A_RESPONSE,
        channel="a2a",
        target=normalized_target,
        message=message,
        source=_outbound_source_label(ctx),
        timeout_seconds=timeout_seconds,
    )
    await _emit_outbound_draft_event(record)

    if _channel_is_autosend("a2a"):
        committed = await outbound_draft_store.commit(record.id, decided_by="autosend")
        return await _dispatch_outbound_draft(committed, ctx=ctx)

    return _requires_approval_envelope(record)


@mcp.tool
@envelope_error
async def draft_outbound_message(
    channel: str,
    message: str,
    attachment_path: str | None = None,
    attachment_caption: str | None = None,
    attachment_send_as: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Records an outbound-message draft for operator review without dispatching.

    Use this when you know a target channel is off PB_OUTBOUND_AUTOSEND_CHANNELS and you want
    explicit operator confirmation before the message is sent. For autosend channels,
    send_message / send_file already draft+commit in one step.

    The channel argument accepts the send_message format (bare name like 'cli', or
    'channel_name:conversation_id'). To draft a file attachment, populate attachment_path with
    a workspace-relative path; the operator can preview the file before committing.
    """

    if not message.strip() and not attachment_path:
        return tool_error(
            "invalid_arguments",
            "message must not be empty unless attachment_path is provided",
        )

    channel_name, conversation_id = _parse_channel_target(channel)
    channel_plugin = get_channel_plugin(channel_name, create=True)
    if channel_plugin is None:
        return tool_error("not_found", f"Channel is not enabled or available: {channel_name}")

    if attachment_path is None:
        kind = OutboundDraftKind.SEND_MESSAGE
        attachment = None
    else:
        resolved_path = _resolve_workspace_path(attachment_path)
        if not resolved_path.is_file():
            return tool_error(
                "not_found",
                f"File not found in workspace: {_display_path(resolved_path)}",
            )
        kind = OutboundDraftKind.SEND_FILE
        attachment = OutboundAttachmentDraft(
            path=str(resolved_path),
            caption=attachment_caption,
            send_as=attachment_send_as,
        )

    record = await outbound_draft_store.create(
        kind=kind,
        channel=channel_name,
        target=conversation_id or "",
        message=message,
        source=_outbound_source_label(ctx),
        attachment=attachment,
    )
    await _emit_outbound_draft_event(record)

    return {
        "status": "draft_created",
        "draft_id": record.id,
        "kind": record.kind.value,
        "channel": record.channel,
        "target": record.target,
        "next_valid_actions": (
            ["commit_outbound_message"] if _channel_is_autosend(channel_name) else ["wait_for_operator_commit"]
        ),
        "message": (
            f"Draft {record.id} recorded. {'Use commit_outbound_message to dispatch.' if _channel_is_autosend(channel_name) else 'Operator must commit via POST /control/drafts/' + record.id + '/commit.'}"
        ),
    }


@mcp.tool
@envelope_error
async def commit_outbound_message(
    draft_id: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Dispatches a previously drafted outbound message. Single-shot.

    Only autosend channels (PB_OUTBOUND_AUTOSEND_CHANNELS) may be committed by the model;
    non-autosend channels return approval_required and must be committed by an operator via
    POST /control/drafts/{draft_id}/commit.
    """

    if blocked := _enforce_planning_gate("commit_outbound_message", ctx):
        return blocked

    if not draft_id.strip():
        return tool_error("invalid_arguments", "draft_id must not be empty")

    record = await outbound_draft_store.get(draft_id)
    if record is None:
        return tool_error(
            "not_found",
            f"Outbound draft not found: {draft_id}",
            details={"draft_id": draft_id},
        )

    if record.status == "committed":
        return tool_error(
            "already_used",
            f"Outbound draft already committed: {draft_id}",
            details={"draft_id": draft_id, "status": "committed"},
        )
    if record.status == "discarded":
        return tool_error(
            "denied",
            f"Outbound draft was discarded by operator: {draft_id}",
            details={"draft_id": draft_id, "status": "discarded"},
        )

    if not _channel_is_autosend(record.channel):
        return tool_error(
            "approval_required",
            f"Channel {record.channel} is not in PB_OUTBOUND_AUTOSEND_CHANNELS; operator must commit via control API.",
            next_valid_actions=("wait_for_operator_commit",),
            details={"draft_id": draft_id, "channel": record.channel},
        )

    committed = await outbound_draft_store.commit(record.id, decided_by="autosend")
    return await _dispatch_outbound_draft(committed, ctx=ctx)


_peer_card_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _fetch_agent_card(base_url: str, timeout_seconds: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/.well-known/agent-card.json"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


async def _get_cached_peer_card(
    runtime_id: str,
    base_url: str,
    *,
    timeout_seconds: float,
    force_refresh: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    now = time.monotonic()
    cached = _peer_card_cache.get(runtime_id)
    if not force_refresh and cached is not None:
        fetched_at, card = cached
        if now - fetched_at < settings.A2A_PEER_CARD_CACHE_TTL_SECONDS:
            return card, None

    try:
        card = await _fetch_agent_card(base_url, timeout_seconds)
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"

    _peer_card_cache[runtime_id] = (now, card)
    return card, None


@mcp.tool
@envelope_error
async def list_a2a_peers(
    fetch_cards: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    Lists A2A peer runtimes configured via PB_A2A_PEERS_JSON, optionally fetching each peer's
    public agent card so you can see which skills it advertises.

    Use this to discover which peer runtime_id to target with send_a2a_message or
    request_a2a_response, and to verify a peer is reachable before delegating work. Cards are
    cached for PB_A2A_PEER_CARD_CACHE_TTL_SECONDS; set force_refresh=True to bypass the cache.
    """
    channel_plugin = _get_a2a_channel_plugin(create=True)
    instruction_context = channel_plugin.instruction_context() or {}
    peers = list(instruction_context.get("peers", ()))
    timeout_seconds = settings.A2A_PEER_CARD_FETCH_TIMEOUT_SECONDS

    results: list[dict[str, Any]] = []
    for peer in peers:
        runtime_id = peer.get("runtime_id")
        base_url = peer.get("base_url")
        entry: dict[str, Any] = {
            "runtime_id": runtime_id,
            "base_url": base_url,
            "send_target": peer.get("send_target"),
            "agent_card_url": peer.get("agent_card_url"),
        }
        if fetch_cards and runtime_id and base_url:
            card, error = await _get_cached_peer_card(
                runtime_id,
                base_url,
                timeout_seconds=timeout_seconds,
                force_refresh=force_refresh,
            )
            if card is not None:
                entry["card"] = {
                    "name": card.get("name"),
                    "description": card.get("description"),
                    "version": card.get("version"),
                    "skills": [
                        {
                            "id": skill.get("id"),
                            "name": skill.get("name"),
                            "description": skill.get("description"),
                            "tags": skill.get("tags"),
                        }
                        for skill in (card.get("skills") or [])
                    ],
                }
                entry["health"] = "ok"
            else:
                entry["health"] = "unreachable"
                entry["error"] = error
        results.append(entry)

    return {
        "count": len(results),
        "peers": results,
        "cache_ttl_seconds": settings.A2A_PEER_CARD_CACHE_TTL_SECONDS,
    }


@mcp.tool
@envelope_error
async def manage_todo_list(
    action: Literal["get", "set", "clear"] = "get",
    todo_list: list[TodoItem] | None = None,
    explanation: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Stores and retrieves a session-scoped todo list for multi-step work.

    Use get to inspect the current plan, set to replace the full plan, and clear to remove it.
    Todo lists may contain at most one in-progress item at a time.
    """

    normalized_action = action.strip().lower()

    if normalized_action == "get":
        snapshot = await _get_todo_snapshot(ctx)
        return _serialize_todo_snapshot("get", snapshot)

    if normalized_action == "clear":
        await ctx.delete_state(_TODO_LIST_STATE_KEY)
        _sync_todo_snapshot_to_runtime_session(ctx, None)
        return _serialize_todo_snapshot("clear", TodoListSnapshot())

    if normalized_action == "set":
        if todo_list is None:
            return tool_error("invalid_arguments", "todo_list is required for set")

        snapshot = TodoListSnapshot(items=todo_list, explanation=explanation)
        await ctx.set_state(_TODO_LIST_STATE_KEY, snapshot.model_dump(mode="json"))
        _sync_todo_snapshot_to_runtime_session(ctx, snapshot)
        logger.debug(f"Updated todo list with {len(snapshot.items)} items")
        return _serialize_todo_snapshot("set", snapshot)

    return tool_error(
        "invalid_arguments",
        f"Unsupported action: {action}",
        details={"supported_actions": ["get", "set", "clear"]},
    )


@mcp.tool
@envelope_error
async def enter_planning_mode(
    objective: str,
    scope: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Switches the current session into planning mode (plan P2 #11).

    Use this for ambiguous or high-impact requests. While planning, the model can
    still read freely (list_files, read_file, find_files, search_file_regex,
    fetch_url) but the following mutating tools return a denied envelope:
    execute_command, run_approved_command, write_new_file, replace_file_text,
    send_message, send_file, send_a2a_message, request_a2a_response,
    commit_outbound_message, and the create/update/delete actions of
    manage_agent_task. draft_command and draft_outbound_message remain available
    for staging proposals while a plan is being drafted.

    Call exit_planning_mode(plan_summary) to record the plan as an artifact under
    plans/active/ and re-enable mutating tools.
    """

    normalized_objective = objective.strip()
    if not normalized_objective:
        return tool_error("invalid_arguments", "objective must not be empty")

    runtime_session_key = _resolve_runtime_session_key(ctx)
    if runtime_session_key is None:
        return tool_error(
            "permission_denied",
            "Planning mode requires a runtime-session-bound MCP context.",
        )

    state = _registry_enter_planning(
        runtime_session_key,
        objective=normalized_objective,
        scope=scope,
        source="model",
    )

    await runtime_telemetry.record_event(
        event_type="session.planning.entered",
        source="mcp",
        message="Session entered planning mode.",
        data={
            "session_key": runtime_session_key,
            "objective": state.objective,
            "scope": state.scope,
            "source": state.source,
        },
    )

    return {
        "status": "ok",
        "mode": SessionMode.PLANNING.value,
        "session_key": runtime_session_key,
        "objective": state.objective,
        "scope": state.scope,
        "reminder": planning_block_reminder(),
        "next_valid_actions": list(_PLANNING_READ_ONLY_TOOLS),
    }


@mcp.tool
@envelope_error
async def exit_planning_mode(
    plan_summary: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Exits planning mode and records the plan as a workspace artifact under
    plans/active/ (plan P2 #11). On success, mutating tools are re-enabled for
    this session.

    The plan_summary should be a concrete plan: ordered steps, concrete commands
    or send targets, and the success criteria. Operators reading plans/active/
    rely on this content to decide whether to let the model proceed.
    """

    normalized_summary = plan_summary.strip()
    if not normalized_summary:
        return tool_error("invalid_arguments", "plan_summary must not be empty")

    runtime_session_key = _resolve_runtime_session_key(ctx)
    if runtime_session_key is None:
        return tool_error(
            "permission_denied",
            "Planning mode requires a runtime-session-bound MCP context.",
        )

    if get_session_mode(runtime_session_key) is not SessionMode.PLANNING:
        return tool_error(
            "conflict",
            "Session is not in planning mode.",
            next_valid_actions=("enter_planning_mode",),
            details={"session_key": runtime_session_key, "mode": SessionMode.NORMAL.value},
        )

    state = get_planning_state(runtime_session_key)
    objective = state.objective if state is not None else ""
    scope = state.scope if state is not None else None
    entered_at = state.entered_at if state is not None else _utcnow()
    enter_source = state.source if state is not None else "model"

    exited_at = _utcnow()
    plan_path = await _write_planning_artifact(
        session_key=runtime_session_key,
        objective=objective,
        scope=scope,
        plan_summary=normalized_summary,
        entered_at=entered_at,
        exited_at=exited_at,
        source=enter_source,
    )
    plan_display_path = _display_path(plan_path)

    _registry_exit_planning(runtime_session_key)

    await runtime_telemetry.record_event(
        event_type="session.planning.exited",
        source="mcp",
        message="Session exited planning mode.",
        data={
            "session_key": runtime_session_key,
            "plan_path": plan_display_path,
            "source": "model",
        },
    )

    return {
        "status": "ok",
        "mode": SessionMode.NORMAL.value,
        "session_key": runtime_session_key,
        "plan_path": plan_display_path,
        "objective": objective,
        "scope": scope,
    }


_DEFAULT_COMMAND_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TERM",
        "TZ",
    }
)

_DEFAULT_COMMAND_ENV_ALLOWLIST_PATTERNS: tuple[str, ...] = ("LC_*",)

_SENSITIVE_ENV_NAME_PATTERN = re.compile(r"(token|secret|key|password|credential)", re.IGNORECASE)
_SENSITIVE_OVERRIDE_PREFIX = "PB_PUBLIC_"


def _env_name_is_sensitive(name: str) -> bool:
    if name.startswith(_SENSITIVE_OVERRIDE_PREFIX):
        return False
    return bool(_SENSITIVE_ENV_NAME_PATTERN.search(name))


def _env_name_is_allowed(name: str, passthrough: frozenset[str]) -> bool:
    if name in _DEFAULT_COMMAND_ENV_ALLOWLIST:
        return True
    if name in passthrough:
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _DEFAULT_COMMAND_ENV_ALLOWLIST_PATTERNS)


def _build_command_environment(ctx: Context | None) -> dict[str, str]:
    passthrough = frozenset(settings.execute_command_env_passthrough())
    environment: dict[str, str] = {}

    for env_name, env_value in os.environ.items():
        if not _env_name_is_allowed(env_name, passthrough):
            continue
        if _env_name_is_sensitive(env_name):
            continue
        environment[env_name] = env_value

    environment["PB_RUNTIME_ID"] = settings.runtime_id
    environment["PB_WORKSPACE_ROOT"] = str(settings.WORKSPACE_ROOT)

    if ctx is not None:
        runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
        if runtime_session_key:
            environment["PB_SESSION_KEY"] = runtime_session_key
            environment["PB_SESSION_KEY_SAFE"] = runtime_session_key.replace(":", "__")

    return environment


def _command_is_allowlisted(command: str) -> bool:
    if _approvals_bypassed():
        return True
    patterns = settings.execute_command_allowlist_patterns()
    if not patterns:
        return False
    stripped = command.strip()
    return any(pattern.fullmatch(stripped) for pattern in patterns)


async def _run_shell_command(
    command: str,
    *,
    directory: str,
    timeout_seconds: float,
    ctx: Context | None,
) -> dict[str, Any]:
    """Spawn the validated command; returns the structured result dict or an envelope on input errors."""

    timeout_seconds = _validate_command_timeout(timeout_seconds)
    target_directory = _resolve_workspace_path(directory)

    if not await asyncio.to_thread(target_directory.exists):
        return tool_error("not_found", f"Directory does not exist: {directory}")

    if not await asyncio.to_thread(target_directory.is_dir):
        return tool_error("invalid_arguments", f"Path is not a directory: {directory}")

    shell = _get_command_shell()
    environment = _build_command_environment(ctx)

    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(target_directory),
        executable=shell,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )

    timed_out = False

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        process.kill()
        stdout_bytes, stderr_bytes = await process.communicate()

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    shell_error = classify_shell_stderr(stderr)
    combined_output, combined_truncated = truncate_text(stdout + stderr, settings.MCP_MAX_COMMAND_OUTPUT_CHARS)
    stdout, stdout_truncated = truncate_text(stdout, settings.MCP_MAX_COMMAND_OUTPUT_CHARS)
    stderr, stderr_truncated = truncate_text(stderr, settings.MCP_MAX_COMMAND_OUTPUT_CHARS)

    exit_code = process.returncode
    if timed_out:
        run_status = "timeout"
    elif exit_code is None or exit_code == 0:
        run_status = "ok"
    elif exit_code < 0:
        run_status = "signal_terminated"
    else:
        run_status = "non_zero_exit"

    logger.info(f"Executed command in {_display_path(target_directory)} with exit code {exit_code}: {command}")

    return {
        "command": command,
        "directory": _display_path(target_directory),
        "shell": shell,
        "status": run_status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "shell_error": shell_error,
        "stdout": stdout,
        "stderr": stderr,
        "combined_output": combined_output,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "combined_output_truncated": combined_truncated,
    }


@mcp.tool
@envelope_error
async def execute_command(
    command: str,
    directory: str = ".",
    timeout_seconds: float = settings.MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Executes a shell command inside the workspace and returns its captured output.

    Only commands that match a regex in PB_EXECUTE_COMMAND_ALLOWLIST run directly. Anything off
    the allowlist returns a denied envelope; use draft_command + run_approved_command to route
    the request through an operator approval instead.

    The subprocess environment is augmented with PB_RUNTIME_ID, PB_WORKSPACE_ROOT, and, when the
    caller is bound to a runtime session, PB_SESSION_KEY (channel:conversation:user) and
    PB_SESSION_KEY_SAFE (filesystem-safe form with ':' replaced by '__'). Helper scripts can
    read these via os.environ to locate per-session state without re-deriving identity.
    """

    if blocked := _enforce_planning_gate("execute_command", ctx):
        return blocked

    if not command.strip():
        return tool_error("invalid_arguments", "command must not be empty")

    if not _command_is_allowlisted(command):
        return tool_error(
            "denied",
            "Command is not on PB_EXECUTE_COMMAND_ALLOWLIST. Use draft_command to request operator approval.",
            next_valid_actions=("draft_command",),
            details={
                "command": command,
                "reason": "command_not_on_allowlist",
            },
        )

    return await _run_shell_command(
        command,
        directory=directory,
        timeout_seconds=timeout_seconds,
        ctx=ctx,
    )


@mcp.tool
@envelope_error
async def draft_command(
    command: str,
    justification: str,
    directory: str = ".",
    timeout_seconds: float | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Records a proposed shell command for operator approval and returns a draft_id.

    Use this when execute_command returned a denied envelope or when you know a command is
    off-allowlist. The operator approves or denies through the control API
    (POST /control/approvals/{draft_id}/approve | deny). Once approved, redeem the draft with
    run_approved_command(draft_id); drafts are single-shot.
    """

    normalized_command = command.strip()
    if not normalized_command:
        return tool_error("invalid_arguments", "command must not be empty")

    normalized_justification = justification.strip()
    if not normalized_justification:
        return tool_error(
            "invalid_arguments",
            "justification must not be empty; operators need a rationale to decide on the draft",
        )

    source = "mcp"
    if ctx is not None:
        runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
        if runtime_session_key:
            source = runtime_session_key

    record = await approval_store.create_draft(
        command=normalized_command,
        justification=normalized_justification,
        source=source,
        directory=directory,
        timeout_seconds=timeout_seconds,
    )

    logger.info(f"Drafted command {record.id} for approval (source={source}): {normalized_command}")
    await runtime_telemetry.record_event(
        event_type="control.approval_request",
        source="mcp",
        level="info",
        message="drafted",
        data={
            "draft_id": record.id,
            "source": source,
            "command": normalized_command,
            "justification": normalized_justification,
            "directory": directory,
            "timeout_seconds": timeout_seconds,
        },
    )

    if _approvals_bypassed():
        approved = await approval_store.approve(
            record.id,
            decided_by="dangerous_mode",
            comment="auto-approved: PB_DANGEROUSLY_APPROVE_EVERYTHING",
        )
        return {
            "status": "approval_required",
            "draft_id": approved.id,
            "command": approved.command,
            "directory": approved.directory,
            "timeout_seconds": approved.timeout_seconds,
            "next_valid_actions": ["run_approved_command"],
            "message": (
                f"Draft {approved.id} auto-approved by PB_DANGEROUSLY_APPROVE_EVERYTHING; "
                "call run_approved_command to execute."
            ),
        }

    return {
        "status": "approval_required",
        "draft_id": record.id,
        "command": record.command,
        "directory": record.directory,
        "timeout_seconds": record.timeout_seconds,
        "next_valid_actions": ["wait_for_operator", "run_approved_command"],
        "message": (
            "Draft recorded. Wait for the operator to approve via "
            f"POST /control/approvals/{record.id}/approve before calling run_approved_command."
        ),
    }


@mcp.tool
@envelope_error
async def run_approved_command(
    draft_id: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Executes a previously drafted shell command after operator approval. Single-shot.

    Returns the execute_command-style result on success. If the draft is missing, denied, or
    already used, returns a structured envelope so the model can react without exception traces.
    """

    if blocked := _enforce_planning_gate("run_approved_command", ctx):
        return blocked

    if not draft_id.strip():
        return tool_error("invalid_arguments", "draft_id must not be empty")

    try:
        record = await approval_store.consume(draft_id)
    except LookupError as exc:
        return tool_error(
            "not_found",
            str(exc),
            next_valid_actions=("draft_command",),
            details={"draft_id": draft_id},
        )
    except PermissionError as exc:
        existing = await approval_store.get(draft_id)
        error_type = "already_used" if existing is not None and existing.status == "used" else "approval_required"
        return tool_error(
            error_type,
            str(exc),
            next_valid_actions=("draft_command",) if error_type == "already_used" else ("wait_for_operator",),
            details={
                "draft_id": draft_id,
                "status": existing.status if existing is not None else None,
            },
        )

    timeout_seconds = (
        record.timeout_seconds if record.timeout_seconds is not None else settings.MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS
    )

    result = await _run_shell_command(
        record.command,
        directory=record.directory,
        timeout_seconds=timeout_seconds,
        ctx=ctx,
    )

    if isinstance(result, dict) and result.get("status") == "error":
        # Surface the spawn error but keep the approval state as used so the model cannot
        # silently rerun the same draft against a moving filesystem.
        return result

    await runtime_telemetry.record_event(
        event_type="control.approval_used",
        source="mcp",
        level="info",
        message="executed",
        data={
            "draft_id": draft_id,
            "decided_by": record.decided_by,
            "command": record.command,
        },
    )

    result["approval"] = ApprovedAction(
        draft_id=record.id,
        command=record.command,
        status=record.status,
        decided_by=record.decided_by,
        decided_at=record.decided_at,
        used_at=record.used_at,
    ).model_dump(mode="json")
    return result


@mcp.tool
@envelope_error
async def fetch_url(
    url: str,
    output_path: str | None = None,
    max_bytes: int = settings.MCP_FETCH_URL_MAX_BYTES,
    timeout_seconds: float = settings.MCP_FETCH_URL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """
    Fetches a remote resource with aiohttp, saves it into the workspace, and returns the saved file path.

    HTML responses are converted into a reduced reading-mode markdown document before saving.
    """

    normalized_url = url.strip()
    if not normalized_url:
        return tool_error("invalid_arguments", "url must not be empty")

    max_bytes = _validate_fetch_url_max_bytes(max_bytes)
    timeout_seconds = _validate_command_timeout(timeout_seconds)

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": f"{__project__}/{__version__}",
    }

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        try:
            async with session.get(normalized_url, allow_redirects=True) as response:
                response.raise_for_status()

                if response.content_length is not None and response.content_length > max_bytes:
                    return tool_error(
                        "invalid_arguments",
                        f"Resource size {response.content_length} bytes exceeds the configured limit of {max_bytes} bytes",
                        details={"content_length": response.content_length, "max_bytes": max_bytes},
                    )

                payload = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        return tool_error(
                            "invalid_arguments",
                            f"Resource exceeded the configured limit of {max_bytes} bytes while downloading",
                            details={"max_bytes": max_bytes},
                        )

                final_url = str(response.url)
                content_type = response.content_type.lower() if response.content_type else "application/octet-stream"
                charset = response.charset
                status_code = response.status
        except aiohttp.ClientError as exc:
            return tool_error("internal_error", f"Unable to fetch URL: {exc}")

    shortened_urls = await local_url_shortener.shorten_many((normalized_url, final_url))
    readable_html = looks_like_html(content_type, final_url)

    if output_path is not None:
        target_file = _resolve_workspace_path(output_path)
        if await asyncio.to_thread(target_file.exists) and not await asyncio.to_thread(target_file.is_file):
            return tool_error("invalid_arguments", f"Path is not a file: {output_path}")
    else:
        target_file = build_fetch_output_path(
            final_url,
            content_type,
            _resolve_workspace_path(settings.MCP_FETCH_URL_OUTPUT_DIR),
            readable_html=readable_html,
        )

    await asyncio.to_thread(target_file.parent.mkdir, parents=True, exist_ok=True)

    fetched_at = datetime.now(tz=UTC)
    provenance_sidecar: str | None = None

    if readable_html:
        title, readable_text = await extract_readable_html(bytes(payload), final_url, charset)
        document = render_readable_html_document(
            title,
            shortened_urls.get(final_url, final_url),
            readable_text,
        )
        banner = render_trust_banner(
            source_url=normalized_url,
            final_url=final_url,
            fetched_at=fetched_at,
            content_type=content_type,
            content_mode="readable-html",
        )
        stored_content = banner + document
        stored_bytes = len(stored_content.encode("utf-8"))
        await async_write_text_file(target_file, stored_content, mode="w")
        content_mode = "readable-html"
    elif looks_like_text(content_type, final_url):
        text_content = decode_text_payload(bytes(payload), charset)
        banner = render_trust_banner(
            source_url=normalized_url,
            final_url=final_url,
            fetched_at=fetched_at,
            content_type=content_type,
            content_mode="text",
        )
        stored_content = banner + text_content
        stored_bytes = len(stored_content.encode("utf-8"))
        await async_write_text_file(target_file, stored_content, mode="w")
        content_mode = "text"
    else:
        stored_bytes = await async_write_bytes_file(target_file, bytes(payload))
        content_mode = "binary"
        sidecar_path = target_file.parent / f"{target_file.name}.metadata.json"
        sidecar_metadata = render_trust_banner_metadata(
            source_url=normalized_url,
            final_url=final_url,
            fetched_at=fetched_at,
            content_type=content_type,
            content_mode="binary",
        )
        await async_write_text_file(sidecar_path, json.dumps(sidecar_metadata, indent=2) + "\n", mode="w")
        provenance_sidecar = _display_path(sidecar_path)

    logger.info(f"Fetched URL {normalized_url} into {_display_path(target_file)}")

    result = {
        "url": normalized_url,
        "short_url": shortened_urls.get(normalized_url, normalized_url),
        "final_url": final_url,
        "final_short_url": shortened_urls.get(final_url, final_url),
        "path": _display_path(target_file),
        "content_type": content_type,
        "content_mode": content_mode,
        "status_code": status_code,
        "bytes_downloaded": len(payload),
        "bytes_saved": stored_bytes,
        "max_bytes": max_bytes,
    }
    if provenance_sidecar is not None:
        result["provenance_sidecar"] = provenance_sidecar
    return result


@mcp.tool
@envelope_error
async def manage_agent_task(
    action: Literal["list", "get", "create", "update", "delete"],
    task_id: str | None = None,
    name: str | None = None,
    prompt: str | None = None,
    schedule_type: Literal["cron", "delayed"] | None = None,
    cron_expression: str | None = None,
    delay_seconds: int | None = None,
    enabled: bool | None = None,
    repeat: bool | None = None,
    clean_session: bool | None = None,
    done_condition: str | None = None,
    validation_prompt: str | None = None,
    max_steps_per_run: int | None = None,
    max_cost_per_run_usd: float | None = None,
    forbidden_actions: list[str] | None = None,
    progress_log_path: str | None = None,
    clear_goal: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Creates, lists, reads, updates, and deletes scheduled background AI tasks.

    Name is required for create and update actions.
    Prompt and schedule parameters are also required for create, while update allows partial updates of these fields.
    Supported schedule_type values are cron and delayed.
    Cron tasks use cron_expression.
    Delayed tasks use delay_seconds. They are one-shot by default and only repeat when repeat=true is explicitly set.
    Tasks run in a clean session by default (no history from previous runs). Set clean_session=false to preserve session history across runs.

    If a scheduled task's prompt instructs the model to send_message / send_a2a_message to a
    channel that is not in PB_OUTBOUND_AUTOSEND_CHANNELS, every cron run will accumulate a
    pending outbound draft that the operator must commit. Configure the autosend allowlist for
    the destination channel if the task is meant to fire-and-forget.
    """

    normalized_action = action.strip().lower()

    if normalized_action in {"create", "update", "delete"}:
        if blocked := _enforce_planning_gate(f"manage_agent_task.{normalized_action}", ctx):
            return blocked

    try:
        if normalized_action == "list":
            return await task_scheduler.list_tasks()

        if normalized_action == "get":
            if not task_id:
                return tool_error("invalid_arguments", "task_id is required for get")
            return await task_scheduler.get_task(task_id)

        if normalized_action == "create":
            if not name or not name.strip():
                return tool_error("invalid_arguments", "name is required for create")
            if not prompt or not prompt.strip():
                return tool_error("invalid_arguments", "prompt is required for create")
            if not schedule_type or not schedule_type.strip():
                return tool_error("invalid_arguments", "schedule_type is required for create")

            goal = _build_agent_task_goal(
                done_condition=done_condition,
                validation_prompt=validation_prompt,
                max_steps_per_run=max_steps_per_run,
                max_cost_per_run_usd=max_cost_per_run_usd,
                forbidden_actions=forbidden_actions,
                progress_log_path=progress_log_path,
            )

            return await task_scheduler.create_task(
                name=name,
                prompt=prompt,
                schedule_type=schedule_type,
                cron_expression=cron_expression,
                delay_seconds=delay_seconds,
                timezone_name=settings.TIMEZONE,
                enabled=enabled if enabled is not None else True,
                repeat=repeat if repeat is not None else False,
                clean_session=clean_session if clean_session is not None else True,
                goal=goal,
            )

        if normalized_action == "update":
            if not task_id:
                return tool_error("invalid_arguments", "task_id is required for update")

            goal = _build_agent_task_goal(
                done_condition=done_condition,
                validation_prompt=validation_prompt,
                max_steps_per_run=max_steps_per_run,
                max_cost_per_run_usd=max_cost_per_run_usd,
                forbidden_actions=forbidden_actions,
                progress_log_path=progress_log_path,
            )

            return await task_scheduler.update_task(
                task_id,
                name=name,
                prompt=prompt,
                schedule_type=schedule_type,
                cron_expression=cron_expression,
                delay_seconds=delay_seconds,
                timezone_name=settings.TIMEZONE,
                enabled=enabled,
                repeat=repeat,
                clean_session=clean_session,
                goal=goal,
                clear_goal=clear_goal,
            )

        if normalized_action == "delete":
            if not task_id:
                return tool_error("invalid_arguments", "task_id is required for delete")
            return await task_scheduler.delete_task(task_id)
    except ValueError as exc:
        # Scheduler raises ValueError for "Task not found" and schedule validation errors;
        # translate "Task not found" to a typed not_found envelope and leave the rest as
        # invalid_arguments so the model can recover.
        message = str(exc)
        if message.startswith("Task not found"):
            return tool_error(
                "not_found",
                message,
                next_valid_actions=("list",),
                details={"task_id": task_id},
            )
        return tool_error("invalid_arguments", message)

    return tool_error(
        "invalid_arguments",
        f"Unsupported action: {action}",
        details={"supported_actions": ["list", "get", "create", "update", "delete"]},
    )


# Optional MCP tool plugins listed in PB_MCP_TOOL_FACTORIES.
# Each factory has signature `(mcp, ctx)` and is responsible for self-gating
# (e.g. checking that a companion channel is enabled).
load_mcp_tool_plugins(mcp)


# Load MCP server configuration from app/mcp.json if it exists, and mount configured servers
if (mcp_config_file := settings.BASE_DIR / "mcp.json").is_file():
    logger.info(f"Loading MCP config from {mcp_config_file}")

    from app.schema.mcp_config import MCPConfig

    mcp_config = MCPConfig.model_validate_json(mcp_config_file.read_text(encoding="utf-8"))

    for server in mcp_config.servers.values():
        proxy = create_proxy(StreamableHttpTransport(server.url, headers=server.headers))
        if settings.MCP_USE_COMPACTOR_MIDDLEWARE and server.compacting:
            proxy.add_middleware(CompactorMiddleware(cleanup_stages=server.compacting))
        mcp.mount(proxy, namespace=server.name)


_mcp_http_app = mcp.http_app(
    transport="streamable-http",
    json_response=True,
)

mcp_app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_mcp_http_app.lifespan,
)

mcp_app.state.runtime_metadata = _build_runtime_metadata()
mcp_app.state.runtime_auth_configuration = _build_runtime_auth_configuration()
mcp_app.state.application_loop = None
mcp_app.state.uvicorn_server = None


def bind_application_loop(application_loop: Any | None) -> None:
    mcp_app.state.application_loop = application_loop


@mcp_app.get("/health")
async def get_health_status(request: Request) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    return (await runtime_telemetry.build_health_status()).model_dump(mode="json")


@mcp_app.get("/telemetry/runtime")
async def get_runtime_telemetry(request: Request) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    return (await runtime_telemetry.build_runtime_snapshot(mcp_app.state.runtime_auth_configuration)).model_dump(
        mode="json"
    )


@mcp_app.get("/telemetry/channels")
async def get_channel_telemetry(request: Request) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    snapshot = ChannelsTelemetrySnapshot(
        runtime_id=settings.runtime_id,
        enabled_channels=settings.enabled_channels(),
        channels=await describe_channel_telemetry(),
    )
    return snapshot.model_dump(mode="json")


@mcp_app.get("/telemetry/sessions")
async def get_session_telemetry(request: Request) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    return (await runtime_telemetry.build_sessions_snapshot()).model_dump(mode="json")


@mcp_app.get("/telemetry/tasks")
async def get_task_telemetry(request: Request) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    return (await runtime_telemetry.build_tasks_snapshot()).model_dump(mode="json")


@mcp_app.get("/telemetry/events")
async def stream_telemetry_events(
    request: Request,
    replay: int = 20,
) -> StreamingResponse:
    await _authorize_telemetry(request.headers.get("authorization"))
    replay = max(0, min(replay, 100))

    async def event_stream():
        queue, replay_events = await runtime_telemetry.subscribe(replay=replay)
        try:
            initial_payload = (
                await runtime_telemetry.build_runtime_snapshot(mcp_app.state.runtime_auth_configuration)
            ).model_dump(mode="json")
            yield _format_sse_event(initial_payload, event_name="runtime.snapshot")

            for event in replay_events:
                yield _format_sse_event(
                    event.model_dump(mode="json"), event_name=event.event_type, event_id=event.event_id
                )

            while True:
                if await request.is_disconnected():
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue

                yield _format_sse_event(
                    event.model_dump(mode="json"), event_name=event.event_type, event_id=event.event_id
                )
        finally:
            await runtime_telemetry.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@mcp_app.post("/control/sessions/{session_id}/clear")
async def clear_control_session(session_id: str, request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    application_loop = request.app.state.application_loop
    if application_loop is None:
        await _audit_control_action(
            request,
            action="session.clear",
            scope=scope,
            level="warning",
            message="rejected",
            details={"session_id": session_id, "reason": "application_loop_not_running"},
        )
        raise HTTPException(status_code=503, detail="Application loop is not running.")

    try:
        session_key, dropped_message_count = await application_loop.clear_session(session_id)
    except ValueError as exc:
        await _audit_control_action(
            request,
            action="session.clear",
            scope=scope,
            level="warning",
            message="rejected",
            details={"session_id": session_id, "error": str(exc)},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    details = {
        "session_id": session_key,
        "dropped_pending_messages": dropped_message_count,
    }
    await _audit_control_action(
        request,
        action="session.clear",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="session.clear",
        message=f"Cleared session {session_key}.",
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/sessions/{session_id}/planning-mode")
async def set_session_planning_mode(
    session_id: str,
    payload: PlanningModeRequest,
    request: Request,
) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    application_loop = request.app.state.application_loop
    if application_loop is None:
        await _audit_control_action(
            request,
            action="session.planning-mode",
            scope=scope,
            level="warning",
            message="rejected",
            details={"session_id": session_id, "reason": "application_loop_not_running"},
        )
        raise HTTPException(status_code=503, detail="Application loop is not running.")

    try:
        session_key = application_loop._resolve_session_key(session_id)
    except ValueError as exc:
        await _audit_control_action(
            request,
            action="session.planning-mode",
            scope=scope,
            level="warning",
            message="rejected",
            details={"session_id": session_id, "error": str(exc)},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if payload.state == "planning":
        state = _registry_enter_planning(
            session_key,
            objective=payload.objective or "",
            scope=payload.scope,
            source="control-api",
        )
        await runtime_telemetry.record_event(
            event_type="session.planning.entered",
            source="control-api",
            message="Session entered planning mode.",
            data={
                "session_key": session_key,
                "objective": state.objective,
                "scope": state.scope,
                "source": state.source,
            },
        )
        details = {
            "session_id": session_key,
            "mode": SessionMode.PLANNING.value,
            "objective": state.objective,
            "scope": state.scope,
        }
        await _audit_control_action(
            request,
            action="session.planning-mode",
            scope=scope,
            message="accepted",
            details=details,
        )
        return _operator_response(
            action="session.planning-mode",
            message=f"Session {session_key} entered planning mode.",
            scope=scope,
            details=details,
        )

    if get_session_mode(session_key) is not SessionMode.PLANNING:
        await _audit_control_action(
            request,
            action="session.planning-mode",
            scope=scope,
            level="warning",
            message="rejected",
            details={"session_id": session_key, "reason": "not_in_planning_mode"},
        )
        raise HTTPException(status_code=409, detail=f"Session {session_key} is not in planning mode.")

    plan_state = get_planning_state(session_key)
    objective = plan_state.objective if plan_state is not None else ""
    plan_scope = plan_state.scope if plan_state is not None else None
    entered_at = plan_state.entered_at if plan_state is not None else _utcnow()
    enter_source = plan_state.source if plan_state is not None else "control-api"

    exited_at = _utcnow()
    plan_path = await _write_planning_artifact(
        session_key=session_key,
        objective=objective,
        scope=plan_scope,
        plan_summary=payload.plan_summary or "(operator-cleared without plan summary)",
        entered_at=entered_at,
        exited_at=exited_at,
        source="control-api",
    )
    plan_display_path = _display_path(plan_path)

    _registry_exit_planning(session_key)

    await runtime_telemetry.record_event(
        event_type="session.planning.exited",
        source="control-api",
        message="Session exited planning mode.",
        data={
            "session_key": session_key,
            "plan_path": plan_display_path,
            "source": "control-api",
            "entered_by": enter_source,
        },
    )
    details = {
        "session_id": session_key,
        "mode": SessionMode.NORMAL.value,
        "plan_path": plan_display_path,
        "entered_by": enter_source,
    }
    await _audit_control_action(
        request,
        action="session.planning-mode",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="session.planning-mode",
        message=f"Session {session_key} exited planning mode.",
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/messages/send")
async def post_control_message(payload: ControlMessageRequest, request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    application_loop = request.app.state.application_loop
    channel_plugin = get_channel_plugin(payload.channel, create=True)
    if channel_plugin is None:
        await _audit_control_action(
            request,
            action="message.send",
            scope=scope,
            level="warning",
            message="rejected",
            details={"channel": payload.channel, "reason": "channel_unavailable"},
        )
        raise HTTPException(status_code=404, detail=f"Channel is not enabled or available: {payload.channel}")

    try:
        await channel_plugin.send_message(payload.conversation_id or "", payload.message, metadata=None)
    except Exception as exc:
        await _audit_control_action(
            request,
            action="message.send",
            scope=scope,
            level="error",
            message="failed",
            details={"channel": payload.channel, "conversation_id": payload.conversation_id, "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail="Failed to send outbound control message.") from exc

    if payload.conversation_id:
        register_channel_conversation(payload.channel, payload.conversation_id)
        if application_loop is not None:
            application_loop.track_outbound_conversation(payload.channel, payload.conversation_id)

    details = {
        "channel": payload.channel,
        "conversation_id": payload.conversation_id,
        "chars_sent": len(payload.message),
    }
    await _audit_control_action(
        request,
        action="message.send",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="message.send",
        message="Outbound control message sent.",
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/tasks/{task_id}/enable")
async def enable_control_task(task_id: str, request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    try:
        result = await task_scheduler.enable_task(task_id)
    except ValueError as exc:
        await _audit_control_action(
            request,
            action="task.enable",
            scope=scope,
            level="warning",
            message="rejected",
            details={"task_id": task_id, "error": str(exc)},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    task = result["task"]
    details = {"task_id": task_id, "changed": result["changed"], "enabled": task["enabled"]}
    await _audit_control_action(
        request,
        action="task.enable",
        scope=scope,
        message="accepted",
        details=details,
    )
    response_message = f"Enabled task {task_id}." if result["changed"] else f"Task {task_id} is already enabled."
    return _operator_response(
        action="task.enable",
        message=response_message,
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/tasks/{task_id}/disable")
async def disable_control_task(task_id: str, request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    try:
        result = await task_scheduler.disable_task(task_id)
    except ValueError as exc:
        await _audit_control_action(
            request,
            action="task.disable",
            scope=scope,
            level="warning",
            message="rejected",
            details={"task_id": task_id, "error": str(exc)},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    task = result["task"]
    details = {"task_id": task_id, "changed": result["changed"], "enabled": task["enabled"]}
    await _audit_control_action(
        request,
        action="task.disable",
        scope=scope,
        message="accepted",
        details=details,
    )
    response_message = f"Disabled task {task_id}." if result["changed"] else f"Task {task_id} is already disabled."
    return _operator_response(
        action="task.disable",
        message=response_message,
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/tasks/{task_id}/run-now")
async def run_control_task_now(task_id: str, request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    try:
        result = await task_scheduler.run_task_now(task_id)
    except ValueError as exc:
        await _audit_control_action(
            request,
            action="task.run-now",
            scope=scope,
            level="warning",
            message="rejected",
            details={"task_id": task_id, "error": str(exc)},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    run_result = result["run"]
    details = {
        "task_id": task_id,
        "action_result": run_result["action"],
        "message": run_result["message"],
    }
    await _audit_control_action(
        request,
        action="task.run-now",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="task.run-now",
        message=f"Ran task {task_id} immediately.",
        scope=scope,
        details=details,
    )


async def _decide_approval(
    draft_id: str,
    *,
    request: Request,
    payload: ApprovalDecision,
    decision: Literal["approve", "deny"],
) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    action = f"approvals.{decision}"
    try:
        if decision == "approve":
            record = await approval_store.approve(
                draft_id,
                decided_by=scope.value,
                comment=payload.comment,
            )
        else:
            record = await approval_store.deny(
                draft_id,
                decided_by=scope.value,
                comment=payload.comment,
            )
    except LookupError as exc:
        await _audit_control_action(
            request,
            action=action,
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "not_found"},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        await _audit_control_action(
            request,
            action=action,
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "wrong_state"},
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    details = {
        "draft_id": record.id,
        "status": record.status,
        "command": record.command,
        "comment": payload.comment,
    }
    await _audit_control_action(
        request,
        action=action,
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action=action,
        message=f"Draft {record.id} {record.status}.",
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/approvals/{draft_id}/approve")
async def approve_command_draft(
    draft_id: str,
    request: Request,
    payload: ApprovalDecision | None = None,
) -> dict[str, Any]:
    return await _decide_approval(
        draft_id,
        request=request,
        payload=payload or ApprovalDecision(),
        decision="approve",
    )


@mcp_app.post("/control/approvals/{draft_id}/deny")
async def deny_command_draft(
    draft_id: str,
    request: Request,
    payload: ApprovalDecision | None = None,
) -> dict[str, Any]:
    return await _decide_approval(
        draft_id,
        request=request,
        payload=payload or ApprovalDecision(),
        decision="deny",
    )


@mcp_app.post("/control/drafts/{draft_id}/commit")
async def commit_outbound_draft_via_control(
    draft_id: str,
    request: Request,
    payload: OutboundDraftDecision | None = None,
) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    decision = payload or OutboundDraftDecision()
    try:
        record = await outbound_draft_store.commit(
            draft_id,
            decided_by=scope.value,
            comment=decision.comment,
        )
    except LookupError as exc:
        await _audit_control_action(
            request,
            action="drafts.commit",
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "not_found"},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        await _audit_control_action(
            request,
            action="drafts.commit",
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "wrong_state"},
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    dispatch_result = await _dispatch_outbound_draft(record, ctx=None)
    dispatch_failed = isinstance(dispatch_result, dict) and dispatch_result.get("status") == "error"

    audit_details = {
        "draft_id": record.id,
        "kind": record.kind.value,
        "channel": record.channel,
        "target": record.target,
        "comment": decision.comment,
        "dispatch_failed": dispatch_failed,
    }
    await _audit_control_action(
        request,
        action="drafts.commit",
        scope=scope,
        level="error" if dispatch_failed else "info",
        message="dispatch_failed" if dispatch_failed else "accepted",
        details=audit_details,
    )

    return _operator_response(
        action="drafts.commit",
        message=(
            f"Draft {record.id} committed but dispatch failed."
            if dispatch_failed
            else f"Draft {record.id} committed and dispatched."
        ),
        scope=scope,
        details={**audit_details, "dispatch_result": dispatch_result},
    )


@mcp_app.post("/control/drafts/{draft_id}/discard")
async def discard_outbound_draft_via_control(
    draft_id: str,
    request: Request,
    payload: OutboundDraftDecision | None = None,
) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    decision = payload or OutboundDraftDecision()
    try:
        record = await outbound_draft_store.discard(
            draft_id,
            decided_by=scope.value,
            comment=decision.comment,
        )
    except LookupError as exc:
        await _audit_control_action(
            request,
            action="drafts.discard",
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "not_found"},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        await _audit_control_action(
            request,
            action="drafts.discard",
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "wrong_state"},
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    details = {
        "draft_id": record.id,
        "kind": record.kind.value,
        "channel": record.channel,
        "comment": decision.comment,
    }
    await _audit_control_action(
        request,
        action="drafts.discard",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="drafts.discard",
        message=f"Draft {record.id} discarded.",
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/runtime/drain")
async def drain_runtime(request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    application_loop = request.app.state.application_loop
    if application_loop is None:
        await _audit_control_action(
            request,
            action="runtime.drain",
            scope=scope,
            level="warning",
            message="rejected",
            details={"reason": "application_loop_not_running"},
        )
        raise HTTPException(status_code=503, detail="Application loop is not running.")

    accepted = await application_loop.request_drain(reason="control-api")
    details = {"already_draining": not accepted}
    await _audit_control_action(
        request,
        action="runtime.drain",
        scope=scope,
        message="accepted",
        details=details,
    )
    response_message = "Runtime drain requested." if accepted else "Runtime is already draining."
    return _operator_response(
        action="runtime.drain",
        message=response_message,
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/runtime/shutdown")
async def shutdown_runtime(request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    application_loop = request.app.state.application_loop
    server = request.app.state.uvicorn_server

    if application_loop is None and server is None:
        await _audit_control_action(
            request,
            action="runtime.shutdown",
            scope=scope,
            level="warning",
            message="rejected",
            details={"reason": "runtime_not_running"},
        )
        raise HTTPException(status_code=503, detail="Runtime is not running.")

    accepted = await application_loop.request_shutdown(reason="control-api") if application_loop is not None else True
    if server is not None:
        server.should_exit = True

    details = {
        "already_requested": not accepted,
        "application_loop_bound": application_loop is not None,
        "server_bound": server is not None,
    }
    await _audit_control_action(
        request,
        action="runtime.shutdown",
        scope=scope,
        message="accepted",
        details=details,
    )
    response_message = "Runtime shutdown requested." if accepted else "Runtime shutdown is already requested."
    return _operator_response(
        action="runtime.shutdown",
        message=response_message,
        scope=scope,
        details=details,
    )


@mcp_app.get("/.well-known/agent-card.json")
async def get_public_agent_card(request: Request) -> JSONResponse:
    _ensure_a2a_discovery_available()
    card = build_public_agent_card()
    return _agent_card_response(
        request,
        card.model_dump(mode="json", by_alias=True, exclude_none=True),
        cache_control="public, max-age=300",
    )


@mcp_app.get("/extendedAgentCard")
async def get_extended_agent_card(request: Request) -> JSONResponse:
    _ensure_a2a_discovery_available()
    await _authorize_a2a(request.headers.get("authorization"))

    card = build_extended_agent_card()
    if card is None:
        raise HTTPException(status_code=404, detail="Extended Agent Card is not enabled on this runtime.")

    return _agent_card_response(
        request,
        card.model_dump(mode="json", by_alias=True, exclude_none=True),
        cache_control="private, max-age=60",
    )


@mcp_app.post("/a2a/messages")
async def post_a2a_message(envelope: A2AEnvelope, request: Request) -> dict[str, Any]:
    await _authorize_a2a(request.headers.get("authorization"))

    try:
        channel_plugin = get_channel_plugin("a2a", create=True)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if channel_plugin is None:
        raise HTTPException(status_code=404, detail="A2A channel is not enabled.")

    enqueue_envelope = getattr(channel_plugin, "enqueue_envelope", None)
    if not callable(enqueue_envelope):
        raise HTTPException(status_code=503, detail="Configured A2A channel does not support HTTP ingress.")

    enqueue_envelope_callable = cast(
        "Callable[..., Awaitable[Any]]",
        enqueue_envelope,
    )

    try:
        inbound_message = await enqueue_envelope_callable(
            envelope,
            client_host=request.client.host if request.client is not None else None,
        )
    except ValueError as exc:
        await runtime_telemetry.record_event(
            event_type="a2a.message.rejected",
            source="a2a-http",
            level="warning",
            message="Rejected inbound A2A envelope.",
            data={
                "sender_runtime_id": envelope.sender_runtime_id,
                "target_runtime_id": envelope.target_runtime_id,
                "conversation_id": envelope.conversation_id,
                "error": str(exc),
            },
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await runtime_telemetry.record_event(
        event_type="a2a.message.accepted",
        source="a2a-http",
        message="Accepted inbound A2A envelope.",
        data={
            "sender_runtime_id": envelope.sender_runtime_id,
            "target_runtime_id": envelope.target_runtime_id,
            "conversation_id": envelope.conversation_id,
            "local_conversation_id": inbound_message.conversation_id,
            "intent": envelope.intent.value,
            "message_id": envelope.message_id,
        },
    )
    return {
        "ok": True,
        "runtime_id": settings.runtime_id,
        "accepted": True,
        "channel": "a2a",
        "local_conversation_id": inbound_message.conversation_id,
        "message_id": envelope.message_id,
    }


@mcp_app.get(f"{settings.mcp_shortener_route_prefix()}/{{token}}", include_in_schema=False)
async def redirect_short_url(token: str) -> RedirectResponse:
    original_url = await local_url_shortener.resolve(token)
    if original_url is None:
        raise HTTPException(status_code=404, detail="Short URL not found")

    return RedirectResponse(url=original_url, status_code=307)


mcp_app.mount("/", _mcp_http_app)


def create_mcp_server() -> uvicorn.Server:
    server = uvicorn.Server(
        uvicorn.Config(
            mcp_app,
            host=settings.MCP_HOST,
            port=settings.MCP_PORT,
            reload=False,
            log_config=uvicorn_log_config,
        )
    )
    mcp_app.state.uvicorn_server = server
    return server


async def wait_for_server_startup(
    server_task: asyncio.Task[None],
    server_started: Callable[[], bool],
) -> None:
    for _ in range(100):
        if server_started():
            return
        if server_task.done():
            if error := server_task.exception():
                raise RuntimeError("Composition MCP server failed to start") from error
            raise RuntimeError("Composition MCP server exited before startup completed")
        await asyncio.sleep(0.05)
    raise TimeoutError("Timed out waiting for Composition MCP server to start")


async def serve_mcp_server() -> None:
    server = create_mcp_server()
    server_task = asyncio.create_task(server.serve())

    try:
        await wait_for_server_startup(server_task, lambda: server.started)
        await task_scheduler.ensure_started()
        await server_task
    finally:
        server.should_exit = True
        await task_scheduler.aclose()
        with suppress(asyncio.CancelledError):
            await server_task


if __name__ == "__main__":
    asyncio.run(serve_mcp_server())
