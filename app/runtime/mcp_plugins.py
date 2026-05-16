"""
Universal loader for optional packages that register MCP tools against the
runtime FastMCP server.

Plugins are discovered through `PB_MCP_TOOL_FACTORIES`, which uses the same
`name=package.module:factory` convention as `PB_CHANNEL_PLUGIN_FACTORIES`.
Each factory has the signature `factory(mcp, ctx)` and is responsible for any
self-gating (e.g. checking that a companion channel is enabled) and for any
filesystem setup it needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from logging import Logger
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from app.core.config import Settings, settings
from app.core.log import logger
from app.util.workspace import resolve_path_within_root

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = ("McpRegistrationContext", "McpToolFactory", "load_mcp_tool_plugins")


@dataclass(frozen=True, slots=True)
class McpRegistrationContext:
    """Handle passed to MCP tool factories at registration time."""

    name: str
    settings: Settings
    logger: Logger

    def resolve_workspace_path(self, path: str | Path) -> Path:
        return resolve_path_within_root(path, self.settings.WORKSPACE_ROOT)


class McpToolFactory(Protocol):
    def __call__(self, mcp: FastMCP, ctx: McpRegistrationContext) -> None: ...


def _load_factory(import_path: str) -> McpToolFactory:
    module_path, separator, attribute_name = import_path.partition(":")
    if not separator or not module_path or not attribute_name:
        raise ValueError(f"Invalid MCP tool factory path: {import_path}")

    module = import_module(module_path)
    factory = getattr(module, attribute_name, None)
    if not callable(factory):
        raise TypeError(f"MCP tool factory is not callable: {import_path}")

    return cast("McpToolFactory", factory)


def load_mcp_tool_plugins(mcp: FastMCP) -> None:
    for plugin_name, import_path in settings.mcp_tool_factories().items():
        factory = _load_factory(import_path)
        ctx = McpRegistrationContext(name=plugin_name, settings=settings, logger=logger)
        factory(mcp, ctx)
        logger.info(f"Loaded MCP tool plugin '{plugin_name}' from {import_path}")
