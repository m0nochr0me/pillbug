"""
Structured tool-result envelopes for MCP tools.

Tools surface user-recoverable errors through `tool_error(...)` returning a
`ToolErrorEnvelope` dict that the model can react to: `status`, `type`, and a
`next_valid_actions` hint. Programmer errors should still raise.

`@envelope_error` is the safety net for tool bodies: it catches `ToolError`
(explicit typed signal from a callee) and bare `ValueError` (legacy raises in
helpers like `resolve_path_within_root`) and converts them into envelope dicts
instead of bubbling raw tracebacks through FastMCP.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = (
    "ToolError",
    "ToolErrorEnvelope",
    "ToolErrorType",
    "envelope_error",
    "tool_error",
)


ToolErrorType = Literal[
    "invalid_arguments",
    "not_found",
    "permission_denied",
    "approval_required",
    "denied",
    "timeout",
    "rate_limited",
    "conflict",
    "already_used",
    "internal_error",
]


class ToolErrorEnvelope(BaseModel):
    status: Literal["error"] = "error"
    type: ToolErrorType
    message: str
    next_valid_actions: tuple[str, ...] = ()
    details: dict[str, Any] = Field(default_factory=dict)


def tool_error(
    type: ToolErrorType,
    message: str,
    *,
    next_valid_actions: tuple[str, ...] = (),
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return ToolErrorEnvelope(
        type=type,
        message=message,
        next_valid_actions=next_valid_actions,
        details=details or {},
    ).model_dump(mode="json")


class ToolError(Exception):
    """Typed error raised from MCP-tool callees; serializes via the envelope."""

    def __init__(
        self,
        type: ToolErrorType,
        message: str,
        *,
        next_valid_actions: tuple[str, ...] = (),
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.type = type
        self.message = message
        self.next_valid_actions = next_valid_actions
        self.details = details or {}

    def envelope(self) -> dict[str, Any]:
        return tool_error(
            self.type,
            self.message,
            next_valid_actions=self.next_valid_actions,
            details=self.details,
        )


def envelope_error(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Decorator: convert ToolError / ValueError raised inside a tool body into envelope dicts.

    Apply INSIDE @mcp.tool. Programmer errors (TypeError, AssertionError, KeyError,
    AttributeError) still propagate so they surface as bugs.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except ToolError as exc:
            return exc.envelope()
        except ValueError as exc:
            return tool_error("invalid_arguments", str(exc))

    return wrapper
