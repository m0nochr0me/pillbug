"""
Entrypoint
"""

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from importlib import import_module

import uvicorn

from app import __banner__, __version__
from app.core.ai import chat_service
from app.core.config import settings
from app.runtime import ApplicationLoop


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
    async with managed_mcp_server():
        print(__banner__)
        if "cli" in settings.enabled_channels():
            print("CLI channel ready. Type /exit to quit.")

        application_loop = ApplicationLoop(chat_service=chat_service)
        await application_loop.run()


def entrypoint() -> None:
    workspace_init()
    parser = argparse.ArgumentParser(description="Pillbug AI Agent Operating System")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()
    asyncio.run(main(args))


def workspace_init() -> None:
    settings.WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
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

    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    entrypoint()
