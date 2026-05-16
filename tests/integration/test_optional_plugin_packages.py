"""Behavior unique to the optional pillbug-memory and pillbug-trigger plugins.

Verifies the per-plugin logic that moved out of `app/mcp.py` when the loader
became universal: memory creates its own workspace dir from settings, and
trigger self-gates on its companion channel being enabled. Both modules are
imported via `pytest.importorskip` so the suite stays green when the optional
extras are not installed.
"""

from __future__ import annotations

import os

# pillbug-trigger's settings module instantiates `BEARER_TOKEN: str` (no default)
# at import time. Provide a value before importorskip resolves the package.
os.environ.setdefault("PB_TRIGGER_BEARER_TOKEN", "test-token")

import pytest  # noqa: E402
from fastmcp import FastMCP  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.log import logger  # noqa: E402
from app.runtime.mcp_plugins import McpRegistrationContext  # noqa: E402

pillbug_memory = pytest.importorskip("pillbug_memory")
pillbug_trigger = pytest.importorskip("pillbug_trigger")


def _ctx(name: str = "test") -> McpRegistrationContext:
    return McpRegistrationContext(name=name, settings=settings, logger=logger)


class TestMemoryPluginRegistration:
    async def test_creates_memory_dir_under_workspace_and_registers_tools(self, isolated_settings):
        memory_root = isolated_settings.WORKSPACE_ROOT / isolated_settings.MEMORY_DIR
        assert not memory_root.exists()

        mcp = FastMCP("test-memory")
        pillbug_memory.register_memory_tools(mcp, _ctx("memory"))

        assert memory_root.is_dir()
        names = {t.name for t in await mcp.list_tools()}
        assert {"memory_list", "memory_get", "memory_add", "memory_update", "memory_delete"} <= names


class TestTriggerPluginRegistration:
    async def test_self_gates_when_channel_disabled(self, isolated_settings, monkeypatch):
        monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli", raising=True)
        mcp = FastMCP("test-trigger-disabled")
        pillbug_trigger.register_trigger_tools(mcp, _ctx("trigger"))
        assert await mcp.list_tools() == []

    async def test_registers_when_channel_enabled(self, isolated_settings, monkeypatch):
        monkeypatch.setattr(settings, "ENABLED_CHANNELS", "cli,trigger", raising=True)
        mcp = FastMCP("test-trigger-enabled")
        pillbug_trigger.register_trigger_tools(mcp, _ctx("trigger"))
        names = [t.name for t in await mcp.list_tools()]
        assert names == ["manage_trigger_sources"]
