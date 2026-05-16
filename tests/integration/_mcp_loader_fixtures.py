"""Spy factories used by `test_mcp_plugin_loader.py`.

The leading underscore keeps pytest from collecting this as a test module while
still allowing import via `tests.integration._mcp_loader_fixtures`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from app.runtime.mcp_plugins import McpRegistrationContext


CALLS: list[dict[str, Any]] = []


def reset() -> None:
    CALLS.clear()


def spy_factory(mcp: FastMCP, ctx: McpRegistrationContext) -> None:
    CALLS.append({"factory": "spy", "name": ctx.name, "ctx": ctx, "mcp": mcp})

    @mcp.tool
    async def spy_tool(value: str) -> dict[str, Any]:
        """Spy MCP tool registered by the test fixture factory."""
        return {"echo": value}


def second_spy_factory(mcp: FastMCP, ctx: McpRegistrationContext) -> None:
    CALLS.append({"factory": "second_spy", "name": ctx.name, "ctx": ctx, "mcp": mcp})

    @mcp.tool
    async def second_spy_tool() -> dict[str, Any]:
        """Second spy tool used to verify multi-plugin ordering."""
        return {"ok": True}


NOT_CALLABLE = "I am a string, not a function"
