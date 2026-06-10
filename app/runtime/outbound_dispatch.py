"""
Outbound draft dispatch.

Runtime-layer home for the send/budget/approval-envelope helpers shared by the MCP
outbound tools (`app.mcp`) and the runtime loop's `/yes` command. The MCP layer imports
from here; the runtime must never import from `app.mcp`.
"""

import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from fastmcp import Context

from app.core.config import settings
from app.core.log import logger
from app.core.telemetry import runtime_telemetry
from app.runtime.channels import get_channel_plugin, register_channel_conversation
from app.runtime.outbound_budget import DEFAULT_NON_CLI_LIMITS, outbound_send_budget
from app.runtime.session_binding import (
    get_runtime_session_for_mcp_session,
    get_runtime_session_origin_metadata,
    record_pending_outbound_injection,
    split_runtime_session_key,
)
from app.schema.control import OutboundDraft, OutboundDraftKind
from app.schema.messages import (
    A2AEnvelope,
    OutboundAttachment,
    build_a2a_origin_routing_metadata,
    extract_a2a_origin_channel_metadata,
    extract_a2a_origin_route,
)
from app.util.tool_result import tool_error
from app.util.workspace import display_path, resolve_path_within_root

# Process-local reference to the running ApplicationLoop, set through
# `app.mcp.bind_application_loop`. Kept here so dispatch can update loop session
# state without a runtime → mcp import.
_application_loop: Any | None = None


def bind_application_loop(application_loop: Any | None) -> None:
    global _application_loop
    _application_loop = application_loop


def _display_path(path: Path) -> str:
    return display_path(path, settings.WORKSPACE_ROOT)


def _resolve_workspace_path(path: str | Path) -> Path:
    return resolve_path_within_root(path, settings.WORKSPACE_ROOT)


def track_outbound_conversation(channel_name: str, conversation_id: str) -> None:
    if not conversation_id:
        return

    register_channel_conversation(channel_name, conversation_id)
    if _application_loop is not None:
        _application_loop.track_outbound_conversation(channel_name, conversation_id)


def get_a2a_channel_plugin(create: bool = True) -> Any:
    channel_plugin = get_channel_plugin("a2a", create=create)
    if channel_plugin is None:
        raise ValueError("A2A channel is not enabled or available")

    return channel_plugin


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


def get_outbound_a2a_metadata(ctx: Context | None) -> dict[str, object] | None:
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


def requires_approval_envelope(record: OutboundDraft) -> dict[str, Any]:
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
    track_outbound_conversation(record.channel, conversation_id)

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
    channel_plugin = get_a2a_channel_plugin(create=True)
    metadata = get_outbound_a2a_metadata(ctx)
    await channel_plugin.send_message(record.target, record.message, metadata=metadata)
    track_outbound_conversation("a2a", record.target)
    return {
        "channel": "a2a",
        "mode": "async",
        "target": record.target,
        "chars_sent": len(record.message),
    }


async def _dispatch_request_a2a_response(record: OutboundDraft, *, ctx: Context | None) -> dict[str, Any]:
    channel_plugin = get_a2a_channel_plugin(create=True)
    send_request = getattr(channel_plugin, "send_request", None)
    if not callable(send_request):
        return tool_error(
            "permission_denied",
            "Configured A2A channel does not support synchronous requests",
        )

    send_request_callable = cast("Callable[..., Awaitable[A2AEnvelope]]", send_request)
    metadata = get_outbound_a2a_metadata(ctx)
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


def outbound_limits_for_channel(channel: str) -> dict[str, int] | None:
    """Resolve the rolling-window budget for `channel`. None = unlimited (plan P2 #15)."""
    configured = settings.outbound_send_limits()
    if channel in configured:
        return configured[channel] or None
    if channel == "cli":
        return None
    return dict(DEFAULT_NON_CLI_LIMITS)


def check_outbound_budget(channel: str, conversation_id: str) -> dict[str, Any] | None:
    limits = outbound_limits_for_channel(channel)
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


async def dispatch_outbound_draft(record: OutboundDraft, *, ctx: Context | None) -> dict[str, Any]:
    if blocked := check_outbound_budget(record.channel, record.target):
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
