"""MCP workspace sandbox: confirm the resolver used by mcp.py rejects escapes.

The MCP tool surface in `app/mcp.py` routes every path through
`resolve_path_within_root` against `settings.WORKSPACE_ROOT`. We test that
boundary here rather than spinning up FastMCP, since the security-load-bearing
piece is the resolver and the live `settings` it points at.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings
from app.util.workspace import resolve_path_within_root


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    """The conftest fixture wires WORKSPACE_ROOT to tmp_workspace/workspace."""
    return settings


def _resolve_via_mcp_settings(path: str) -> Path:
    return resolve_path_within_root(path, settings.WORKSPACE_ROOT)


class TestWorkspaceRootSandbox:
    def test_relative_path_is_accepted(self, workspace_settings):
        resolved = _resolve_via_mcp_settings("notes.txt")
        assert resolved.is_relative_to(settings.WORKSPACE_ROOT)

    def test_parent_traversal_is_rejected(self, workspace_settings):
        with pytest.raises(ValueError, match="escapes workspace root"):
            _resolve_via_mcp_settings("../escape.txt")

    def test_absolute_outside_path_is_rejected(self, workspace_settings, tmp_path: Path):
        outside = tmp_path / "outside.txt"
        with pytest.raises(ValueError):
            _resolve_via_mcp_settings(str(outside))

    def test_symlink_escape_is_rejected(self, workspace_settings, tmp_path: Path):
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "leaked.txt").write_text("nope", encoding="utf-8")
        (settings.WORKSPACE_ROOT / "leak").symlink_to(outside_dir)

        with pytest.raises(ValueError):
            _resolve_via_mcp_settings("leak/leaked.txt")
