"""
Shared helpers for the composition MCP server package: workspace path resolution,
argument validators, and approval/session gating used across tool and route modules.
"""

from pathlib import Path

from fastmcp import Context

from app.core.config import settings
from app.runtime.session_binding import (
    get_runtime_session_for_mcp_session,
    get_runtime_session_origin_metadata,
    split_runtime_session_key,
)
from app.schema.messages import A2ATarget, extract_a2a_origin_route
from app.util.workspace import display_path, resolve_path_within_root


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


def _validate_fetch_url_max_bytes(max_bytes: int) -> int:
    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")

    return min(max_bytes, settings.MCP_FETCH_URL_MAX_BYTES)


def _parse_channel_target(channel: str) -> tuple[str, str]:
    channel_name, separator, conversation_id = channel.strip().partition(":")
    if not channel_name:
        raise ValueError("channel must not be empty")

    if not separator:
        return channel_name, ""

    if not conversation_id:
        raise ValueError("channel targets using ':' must include a destination after the channel name")

    return channel_name, conversation_id


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


def _resolve_runtime_session_key(ctx: Context | None) -> str | None:
    if ctx is None:
        return None
    return get_runtime_session_for_mcp_session(ctx.session_id)
