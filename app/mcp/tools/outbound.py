"""Outbound send/draft MCP tools and A2A peer discovery."""

import json
import time
from typing import Any

import aiohttp
from fastmcp import Context

from app.core.config import settings
from app.core.telemetry import runtime_telemetry
from app.mcp.server import (
    mcp,
)
from app.mcp.shared import (
    _assert_not_echoing_a2a_origin,
    _channel_is_autosend,
    _display_path,
    _normalize_a2a_target,
    _outbound_source_label,
    _parse_channel_target,
    _resolve_workspace_path,
)
from app.mcp.tools.planning import _enforce_planning_gate
from app.runtime.approvals import outbound_draft_store
from app.runtime.channels import get_channel_plugin
from app.runtime.command_execution import (
    validate_command_timeout as _validate_command_timeout,
)
from app.runtime.outbound_dispatch import (
    dispatch_outbound_draft as _dispatch_outbound_draft,
)
from app.runtime.outbound_dispatch import (
    get_a2a_channel_plugin as _get_a2a_channel_plugin,
)
from app.runtime.outbound_dispatch import (
    requires_approval_envelope as _requires_approval_envelope,
)
from app.schema.control import (
    OutboundAttachmentDraft,
    OutboundDraft,
    OutboundDraftKind,
)
from app.util.tool_result import envelope_error, tool_error


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
