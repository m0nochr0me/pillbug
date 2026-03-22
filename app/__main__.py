"""
Entrypoint
"""

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from importlib import import_module
from typing import TYPE_CHECKING

from app import __banner__, __version__
from app.core.config import settings
from app.core.log import logger

# isort: split

from app.core.ai import chat_service
from app.runtime import ApplicationLoop
from app.runtime.pipeline import ensure_security_patterns_file
from app.runtime.scheduler import task_scheduler

if TYPE_CHECKING:
    import uvicorn


def get_mcp_server_factory() -> Callable[[], uvicorn.Server]:
    return import_module("app.mcp").create_mcp_server


async def wait_for_mcp_server_startup(
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


@asynccontextmanager
async def managed_mcp_server() -> AsyncIterator[None]:
    server = get_mcp_server_factory()()
    server_task = asyncio.create_task(server.serve())

    try:
        await wait_for_mcp_server_startup(server_task, lambda: server.started)
        yield
    finally:
        server.should_exit = True
        with suppress(asyncio.CancelledError):
            await server_task


async def main(*args) -> None:
    async with managed_mcp_server(), managed_scheduler():
        print(__banner__)
        if "cli" in settings.enabled_channels():
            print("CLI channel ready. Type /exit to quit.")
        logger.info(f"Runtime identity: {settings.runtime_id}")
        logger.info(f"Active channels: {', '.join(settings.enabled_channels())}")

        application_loop = ApplicationLoop(chat_service=chat_service)
        mcp_module = import_module("app.mcp")
        mcp_module.bind_application_loop(application_loop)

        try:
            await application_loop.run()
            if application_loop.is_draining and not application_loop.is_shutdown_requested:
                logger.info("Application loop drained; waiting for shutdown request.")
                await application_loop.wait_for_shutdown()
        finally:
            mcp_module.bind_application_loop(None)


def entrypoint() -> None:
    workspace_init()
    parser = argparse.ArgumentParser(description="Pillbug AI Agent Operating System")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()
    asyncio.run(main(args))


@asynccontextmanager
async def managed_scheduler() -> AsyncIterator[None]:
    await task_scheduler.ensure_started()

    try:
        yield
    finally:
        await task_scheduler.aclose()


def workspace_init() -> None:
    settings.BASE_DIR.mkdir(parents=True, exist_ok=True)
    settings.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    settings.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    settings.TASKS_DIR.mkdir(parents=True, exist_ok=True)
    settings.ensure_runtime_identity()

    ensure_security_patterns_file()

    if not (settings.WORKSPACE_ROOT / "AGENTS.md").is_file():
        (settings.WORKSPACE_ROOT / "AGENTS.md").write_text(
            "---\n"
            "name: Assistant\n"
            "description: You are Joi (she/her) a friendly AI assistant.\n"
            "output_format: Plain text, suitable for direct speech. Avoid any markup, emoji and formatting.\n"
            "---\n",
            encoding="utf-8",
        )
        print(
            f"Initialized workspace at {settings.WORKSPACE_ROOT}\n"
            f"Please review your workspace files and customize your assistant's personality and settings as desired."
        )
        sys.exit(0)


if __name__ == "__main__":
    entrypoint()
