"""MCP tool plugin loader: factory resolution, env-driven dispatch, ctx threading."""

from __future__ import annotations

import pytest
from fastmcp import FastMCP

from app.core.config import settings
from app.core.log import logger
from app.runtime.mcp_plugins import (
    McpRegistrationContext,
    _load_factory,
    load_mcp_tool_plugins,
)
from tests.integration import _mcp_loader_fixtures as fixtures

_FIXTURE_PATH = "tests.integration._mcp_loader_fixtures"


@pytest.fixture(autouse=True)
def reset_spy_state():
    fixtures.reset()
    yield
    fixtures.reset()


class TestLoadFactory:
    def test_well_formed_path_resolves(self):
        factory = _load_factory(f"{_FIXTURE_PATH}:spy_factory")
        assert factory is fixtures.spy_factory

    def test_missing_separator_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid MCP tool factory path"):
            _load_factory(f"{_FIXTURE_PATH}.spy_factory")

    def test_empty_module_or_attribute_is_rejected(self):
        with pytest.raises(ValueError):
            _load_factory(":spy_factory")
        with pytest.raises(ValueError):
            _load_factory(f"{_FIXTURE_PATH}:")

    def test_non_callable_attribute_is_rejected(self):
        with pytest.raises(TypeError, match="not callable"):
            _load_factory(f"{_FIXTURE_PATH}:NOT_CALLABLE")


class TestLoadMcpToolPlugins:
    async def test_empty_config_registers_nothing(self, monkeypatch):
        monkeypatch.setattr(settings, "MCP_TOOL_FACTORIES", "", raising=True)
        mcp = FastMCP("test-empty")
        load_mcp_tool_plugins(mcp)
        assert fixtures.CALLS == []
        assert await mcp.list_tools() == []

    async def test_factory_invoked_with_ctx(self, monkeypatch):
        monkeypatch.setattr(
            settings,
            "MCP_TOOL_FACTORIES",
            f"spy={_FIXTURE_PATH}:spy_factory",
            raising=True,
        )
        mcp = FastMCP("test-ctx")
        load_mcp_tool_plugins(mcp)

        assert len(fixtures.CALLS) == 1
        call = fixtures.CALLS[0]
        assert call["factory"] == "spy"
        assert call["name"] == "spy"
        assert call["mcp"] is mcp

        ctx = call["ctx"]
        assert isinstance(ctx, McpRegistrationContext)
        assert ctx.name == "spy"
        assert ctx.settings is settings
        assert ctx.logger is logger

        tool_names = sorted(t.name for t in await mcp.list_tools())
        assert tool_names == ["spy_tool"]

    async def test_multiple_factories_invoked_in_declared_order(self, monkeypatch):
        monkeypatch.setattr(
            settings,
            "MCP_TOOL_FACTORIES",
            f"spy={_FIXTURE_PATH}:spy_factory,second={_FIXTURE_PATH}:second_spy_factory",
            raising=True,
        )
        mcp = FastMCP("test-multi")
        load_mcp_tool_plugins(mcp)

        assert [c["name"] for c in fixtures.CALLS] == ["spy", "second"]
        tool_names = sorted(t.name for t in await mcp.list_tools())
        assert tool_names == ["second_spy_tool", "spy_tool"]

    def test_unknown_module_propagates_import_error(self, monkeypatch):
        monkeypatch.setattr(
            settings,
            "MCP_TOOL_FACTORIES",
            "ghost=pillbug_does_not_exist:nope",
            raising=True,
        )
        mcp = FastMCP("test-missing")
        with pytest.raises(ModuleNotFoundError):
            load_mcp_tool_plugins(mcp)

    def test_malformed_env_entry_is_rejected_at_parse(self, monkeypatch):
        monkeypatch.setattr(settings, "MCP_TOOL_FACTORIES", "broken-no-equals", raising=True)
        with pytest.raises(ValueError, match="PB_MCP_TOOL_FACTORIES entries"):
            settings.mcp_tool_factories()


class TestMcpRegistrationContext:
    def test_resolve_workspace_path_under_root(self, isolated_settings):
        ctx = McpRegistrationContext(name="x", settings=isolated_settings, logger=logger)
        resolved = ctx.resolve_workspace_path("notes.txt")
        assert resolved.is_relative_to(isolated_settings.WORKSPACE_ROOT)

    def test_resolve_workspace_path_rejects_traversal(self, isolated_settings):
        ctx = McpRegistrationContext(name="x", settings=isolated_settings, logger=logger)
        with pytest.raises(ValueError, match="escapes workspace root"):
            ctx.resolve_workspace_path("../escape.txt")
