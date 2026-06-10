"""
Composition MCP server instances and bootstrap: the FastMCP server, the FastAPI app
that fronts it, plugin/proxy mounting, and the uvicorn create/serve helpers.
"""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.middleware.logging import LoggingMiddleware

from app import __project__
from app.core.config import settings
from app.core.log import logger, uvicorn_log_config
from app.mcp.auth import _build_runtime_auth_configuration, _build_runtime_metadata
from app.middleware.compactor import CompactorMiddleware
from app.middleware.telemetry import TelemetryMiddleware
from app.runtime import outbound_dispatch
from app.runtime.mcp_plugins import load_mcp_tool_plugins
from app.runtime.scheduler import task_scheduler

mcp = FastMCP(f"{__project__}-composition-server")
mcp.add_middleware(LoggingMiddleware(include_payloads=True, max_payload_length=1000))
mcp.add_middleware(TelemetryMiddleware())


# Optional MCP tool plugins listed in PB_MCP_TOOL_FACTORIES.
# Each factory has signature `(mcp, ctx)` and is responsible for self-gating
# (e.g. checking that a companion channel is enabled).
load_mcp_tool_plugins(mcp)


# Load MCP server configuration from app/mcp.json if it exists, and mount configured servers
if (mcp_config_file := settings.BASE_DIR / "mcp.json").is_file():
    logger.info(f"Loading MCP config from {mcp_config_file}")

    from app.schema.mcp_config import MCPConfig

    mcp_config = MCPConfig.model_validate_json(mcp_config_file.read_text(encoding="utf-8"))

    for server in mcp_config.servers.values():
        proxy = create_proxy(StreamableHttpTransport(server.url, headers=server.headers))
        if settings.MCP_USE_COMPACTOR_MIDDLEWARE and server.compacting:
            proxy.add_middleware(CompactorMiddleware(cleanup_stages=server.compacting))
        mcp.mount(proxy, namespace=server.name)


_mcp_http_app = mcp.http_app(
    transport="streamable-http",
    json_response=True,
)

mcp_app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_mcp_http_app.lifespan,
)

mcp_app.state.runtime_metadata = _build_runtime_metadata()
mcp_app.state.runtime_auth_configuration = _build_runtime_auth_configuration()
mcp_app.state.application_loop = None
mcp_app.state.uvicorn_server = None


def bind_application_loop(application_loop: Any | None) -> None:
    mcp_app.state.application_loop = application_loop
    outbound_dispatch.bind_application_loop(application_loop)


def create_mcp_server() -> uvicorn.Server:
    server = uvicorn.Server(
        uvicorn.Config(
            mcp_app,
            host=settings.MCP_HOST,
            port=settings.MCP_PORT,
            reload=False,
            log_config=uvicorn_log_config,
        )
    )
    mcp_app.state.uvicorn_server = server
    return server


async def wait_for_server_startup(
    server_task: asyncio.Task[None],
    server_started: Callable[[], bool],
) -> None:
    for _ in range(100):
        if server_started():
            return
        if server_task.done():
            if error := server_task.exception():
                raise RuntimeError("Composition MCP server failed to start") from error
            raise RuntimeError("Composition MCP server exited before startup completed")
        await asyncio.sleep(0.05)
    raise TimeoutError("Timed out waiting for Composition MCP server to start")


async def serve_mcp_server() -> None:
    server = create_mcp_server()
    server_task = asyncio.create_task(server.serve())

    try:
        await wait_for_server_startup(server_task, lambda: server.started)
        await task_scheduler.ensure_started()
        await server_task
    finally:
        server.should_exit = True
        await task_scheduler.aclose()
        with suppress(asyncio.CancelledError):
            await server_task
