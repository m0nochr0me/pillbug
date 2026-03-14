"""
Entrypoint
"""

import argparse
import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from importlib import import_module

import uvicorn

from app import __version__
from app.core.ai import GeminiChatService, chat_service
from app.core.config import settings


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
        # TODO: Implement application loop
        print(f"Hello from {__version__}!")
        await chat_service.create_chat()
        response = await chat_service.send_message("What is the meaning of life, the universe, and everything?")
        print(response.text, response.usage_metadata.model_dump_json() if response.usage_metadata else "No usage metadata")


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
            "# Assistant\n\nYou are Joi(she/her) a smiley virtual assistant.\n",
            encoding="utf-8",
        )

    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    entrypoint()
