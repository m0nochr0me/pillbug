"""Runtime info MCP tool."""

from typing import Any

from app.mcp.auth import (
    _build_runtime_metadata,
)
from app.mcp.server import (
    mcp,
)


@mcp.tool
def get_runtime_info() -> dict[str, Any]:
    """
    Provides a runtime info
    """
    return _build_runtime_metadata().model_dump(mode="json")
