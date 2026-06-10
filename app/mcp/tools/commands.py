"""Command execution MCP tools: allowlisted execute plus draft/approve flow."""

from typing import Any

from fastmcp import Context

from app.core.config import settings
from app.core.log import logger
from app.core.telemetry import runtime_telemetry
from app.mcp.server import (
    mcp,
)
from app.mcp.shared import (
    _approvals_bypassed,
)
from app.mcp.tools.planning import _enforce_planning_gate
from app.runtime.approvals import approval_store
from app.runtime.command_execution import (
    run_shell_command as _run_shell_command,
)
from app.runtime.session_binding import (
    get_runtime_session_for_mcp_session,
)
from app.schema.control import (
    ApprovedAction,
)
from app.util.tool_result import envelope_error, tool_error


def _command_is_allowlisted(command: str) -> bool:
    if _approvals_bypassed():
        return True
    patterns = settings.execute_command_allowlist_patterns()
    if not patterns:
        return False
    stripped = command.strip()
    return any(pattern.fullmatch(stripped) for pattern in patterns)


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
