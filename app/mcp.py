"""
Composition MCP Server
"""

import asyncio
import os
import re
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastmcp import Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy

from app import __project__, __version__
from app.core.config import settings
from app.core.log import logger, uvicorn_log_config
from app.core.url_shortener import local_url_shortener
from app.middleware.compactor import CompactorMiddleware
from app.runtime.channels import get_channel_plugin
from app.runtime.scheduler import task_scheduler
from app.schema.todo import TodoItem, TodoListSnapshot

__all__ = ("create_mcp_server", "mcp", "mcp_app")

mcp = FastMCP(f"{__project__}-composition-server")

_TODO_LIST_STATE_KEY = "todo_list"


def _display_path(path: Path) -> str:
    if path == settings.WORKSPACE_ROOT:
        return "."

    return str(path.relative_to(settings.WORKSPACE_ROOT))


def _resolve_workspace_path(path: str | Path) -> Path:
    raw_path = Path(path)
    candidate = raw_path if raw_path.is_absolute() else settings.WORKSPACE_ROOT / raw_path
    resolved = candidate.resolve()

    if not resolved.is_relative_to(settings.WORKSPACE_ROOT):
        raise ValueError(f"Path escapes workspace root: {path}")

    return resolved


def _validate_page_size(page_size: int) -> int:
    if page_size < 1:
        raise ValueError("page_size must be at least 1")

    return min(page_size, settings.MCP_MAX_PAGE_SIZE)


def _validate_max_results(max_results: int) -> int:
    if max_results < 1:
        raise ValueError("max_results must be at least 1")

    return min(max_results, settings.MCP_MAX_SEARCH_RESULTS)


def _validate_command_timeout(timeout_seconds: float) -> float:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0")

    return min(timeout_seconds, settings.MCP_MAX_COMMAND_TIMEOUT_SECONDS)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text(path: Path, content: str, *, mode: str) -> int:
    with path.open(mode, encoding="utf-8") as file:
        return file.write(content)


def _is_hidden(relative_path: Path) -> bool:
    return any(part.startswith(".") for part in relative_path.parts)


def _truncate_output(
    text: str,
    limit: int = settings.MCP_MAX_COMMAND_OUTPUT_CHARS,
) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False

    omitted_chars = len(text) - limit
    suffix = f"\n... truncated {omitted_chars} characters"
    truncated = text[: max(limit - len(suffix), 0)] + suffix
    return truncated, True


def _get_command_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell and Path(shell).is_file():
        return shell

    return "/bin/sh"


def _parse_channel_target(channel: str) -> tuple[str, str]:
    channel_name, separator, conversation_id = channel.strip().partition(":")
    if not channel_name:
        raise ValueError("channel must not be empty")

    if not separator:
        return channel_name, ""

    if not conversation_id:
        raise ValueError("channel targets using ':' must include a destination after the channel name")

    return channel_name, conversation_id


async def _get_todo_snapshot(ctx: Context) -> TodoListSnapshot:
    state = await ctx.get_state(_TODO_LIST_STATE_KEY)
    if state is None:
        return TodoListSnapshot()

    return TodoListSnapshot.model_validate(state)


def _serialize_todo_snapshot(action: str, snapshot: TodoListSnapshot) -> dict[str, Any]:
    return {
        "action": action,
        "items": [item.model_dump(mode="json") for item in snapshot.items],
        "total": len(snapshot.items),
        "counts": snapshot.counts,
        "explanation": snapshot.explanation,
        "updated_at": snapshot.updated_at.isoformat(),
    }


@mcp.resource("resource://info")
def get_greeting() -> str:
    """
    Provides an info about the CLI application.
    """
    return f"{__project__} v{__version__} - AI assistant for terminal"


@mcp.tool
async def list_files(
    directory: str = ".",
    include_hidden: bool = False,
) -> dict[str, Any]:
    """
    Lists files and directories directly under a workspace-relative directory.
    """

    target_directory = _resolve_workspace_path(directory)

    if not await asyncio.to_thread(target_directory.exists):
        raise ValueError(f"Directory does not exist: {directory}")

    if not await asyncio.to_thread(target_directory.is_dir):
        raise ValueError(f"Path is not a directory: {directory}")

    def build_entries() -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []

        for entry in sorted(target_directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            relative_path = entry.relative_to(settings.WORKSPACE_ROOT)
            if not include_hidden and _is_hidden(relative_path):
                continue

            entry_type = "directory" if entry.is_dir() else "file"
            entries.append(
                {
                    "name": entry.name,
                    "path": _display_path(entry),
                    "type": entry_type,
                    "size": entry.stat().st_size if entry.is_file() else None,
                }
            )

        return entries

    entries = await asyncio.to_thread(build_entries)
    logger.debug(f"Listed {len(entries)} entries in {_display_path(target_directory)}")

    return {
        "directory": _display_path(target_directory),
        "entries": entries,
        "count": len(entries),
    }


@mcp.tool
async def read_file(
    path: str,
    start_line: int = 1,
    page_size: int = settings.MCP_DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """
    Reads a UTF-8 text file from the workspace with line-based pagination.
    """

    if start_line < 1:
        raise ValueError("start_line must be at least 1")

    page_size = _validate_page_size(page_size)
    target_file = _resolve_workspace_path(path)

    if not await asyncio.to_thread(target_file.exists):
        raise ValueError(f"File does not exist: {path}")

    if not await asyncio.to_thread(target_file.is_file):
        raise ValueError(f"Path is not a file: {path}")

    content = await asyncio.to_thread(_read_text, target_file)
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    start_index = min(start_line - 1, total_lines)
    page_lines = lines[start_index : start_index + page_size]
    end_line = start_index + len(page_lines)

    logger.debug(f"Read {len(page_lines)} lines from {_display_path(target_file)} starting at line {start_line}")

    return {
        "path": _display_path(target_file),
        "start_line": start_line,
        "end_line": end_line,
        "page_size": page_size,
        "total_lines": total_lines,
        "has_more": end_line < total_lines,
        "content": "".join(page_lines),
    }


@mcp.tool
async def write_new_file(
    path: str,
    content: str,
    make_parents: bool = True,
) -> dict[str, Any]:
    """
    Creates a new UTF-8 text file in the workspace and fails if it already exists.
    """

    target_file = _resolve_workspace_path(path)

    if await asyncio.to_thread(target_file.exists):
        raise ValueError(f"File already exists: {path}")

    if make_parents:
        await asyncio.to_thread(target_file.parent.mkdir, parents=True, exist_ok=True)
    elif not await asyncio.to_thread(target_file.parent.exists):
        raise ValueError(f"Parent directory does not exist: {_display_path(target_file.parent)}")

    chars_written = await asyncio.to_thread(_write_text, target_file, content, mode="x")
    logger.info(f"Created file {_display_path(target_file)}")

    return {
        "path": _display_path(target_file),
        "chars_written": chars_written,
    }


@mcp.tool
async def replace_file_text(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    expected_occurrences: int | None = None,
) -> dict[str, Any]:
    """
    Replaces literal text inside an existing UTF-8 text file.
    """

    if not old_text:
        raise ValueError("old_text must not be empty")

    target_file = _resolve_workspace_path(path)

    if not await asyncio.to_thread(target_file.exists):
        raise ValueError(f"File does not exist: {path}")

    if not await asyncio.to_thread(target_file.is_file):
        raise ValueError(f"Path is not a file: {path}")

    content = await asyncio.to_thread(_read_text, target_file)
    occurrences = content.count(old_text)

    if occurrences == 0:
        raise ValueError("old_text was not found in the file")

    if expected_occurrences is not None and occurrences != expected_occurrences:
        raise ValueError(f"Expected {expected_occurrences} occurrences of old_text, but found {occurrences}")

    replacement_count = occurrences if replace_all else 1
    updated_content = content.replace(old_text, new_text, replacement_count)
    await asyncio.to_thread(_write_text, target_file, updated_content, mode="w")
    logger.info(f"Replaced {replacement_count} occurrence(s) in {_display_path(target_file)}")

    return {
        "path": _display_path(target_file),
        "occurrences_found": occurrences,
        "occurrences_replaced": replacement_count,
    }


@mcp.tool
async def search_file_regex(
    path: str,
    pattern: str,
    max_results: int = 50,
) -> dict[str, Any]:
    """
    Searches a UTF-8 text file line by line using a regular expression.
    """

    target_file = _resolve_workspace_path(path)
    max_results = _validate_max_results(max_results)

    if not await asyncio.to_thread(target_file.exists):
        raise ValueError(f"File does not exist: {path}")

    if not await asyncio.to_thread(target_file.is_file):
        raise ValueError(f"Path is not a file: {path}")

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc

    content = await asyncio.to_thread(_read_text, target_file)
    matches: list[dict[str, Any]] = []
    truncated = False

    for line_number, line in enumerate(content.splitlines(), start=1):
        for match in regex.finditer(line):
            matches.append(
                {
                    "line": line_number,
                    "start_column": match.start() + 1,
                    "end_column": match.end(),
                    "match": match.group(0),
                    "line_text": line,
                }
            )

            if len(matches) >= max_results:
                truncated = True
                break

        if truncated:
            break

    logger.debug(f"Found {len(matches)} regex matches in {_display_path(target_file)}")

    return {
        "path": _display_path(target_file),
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
        "truncated": truncated,
    }


@mcp.tool
async def find_files(
    pattern: str,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """
    Finds workspace files by glob pattern relative to the workspace root.
    """

    def run_glob() -> list[str]:
        matches: list[str] = []

        for candidate in sorted(settings.WORKSPACE_ROOT.glob(pattern)):
            if not candidate.is_file():
                continue

            relative_path = candidate.relative_to(settings.WORKSPACE_ROOT)
            if not include_hidden and _is_hidden(relative_path):
                continue

            matches.append(str(relative_path))

        return matches

    matches = await asyncio.to_thread(run_glob)
    logger.debug(f"Glob pattern {pattern} matched {len(matches)} files")

    return {
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
    }


@mcp.tool
async def send_message(
    channel: str,
    message: str,
) -> dict[str, Any]:
    """
    Sends a direct outbound message to a configured channel.
    Intended for subagents and scheduled tasks to proactively send messages outside of an active conversation turn.

    The channel argument accepts either a bare channel name for default destinations such as cli,
    or a session-style target in the form channel_name:conversation_id such as telegram:123456789.
    """

    if not message.strip():
        raise ValueError("message must not be empty")

    channel_name, conversation_id = _parse_channel_target(channel)
    channel_plugin = get_channel_plugin(channel_name, create=True)
    if channel_plugin is None:
        raise ValueError(f"Channel is not enabled or available: {channel_name}")

    await channel_plugin.send_message(conversation_id, message)
    logger.info(f"Sent outbound message via channel={channel_name} destination={conversation_id or '<default>'}")

    return {
        "channel": channel_name,
        "conversation_id": conversation_id or None,
        "chars_sent": len(message),
    }


@mcp.tool
async def manage_todo_list(
    action: Literal["get", "set", "clear"] = "get",
    todo_list: list[TodoItem] | None = None,
    explanation: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Stores and retrieves a session-scoped todo list for multi-step work.

    Use get to inspect the current plan, set to replace the full plan, and clear to remove it.
    Todo lists may contain at most one in-progress item at a time.
    """

    normalized_action = action.strip().lower()

    if normalized_action == "get":
        snapshot = await _get_todo_snapshot(ctx)
        return _serialize_todo_snapshot("get", snapshot)

    if normalized_action == "clear":
        await ctx.delete_state(_TODO_LIST_STATE_KEY)
        return _serialize_todo_snapshot("clear", TodoListSnapshot())

    if normalized_action == "set":
        if todo_list is None:
            raise ValueError("todo_list is required for set")

        snapshot = TodoListSnapshot(items=todo_list, explanation=explanation)
        await ctx.set_state(_TODO_LIST_STATE_KEY, snapshot.model_dump(mode="json"))
        logger.debug(f"Updated todo list with {len(snapshot.items)} items")
        return _serialize_todo_snapshot("set", snapshot)

    raise ValueError(f"Unsupported action: {action}")


@mcp.tool
async def execute_command(
    command: str,
    directory: str = ".",
    timeout_seconds: float = settings.MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """
    Executes a shell command inside the workspace and returns its captured output.
    """

    if not command.strip():
        raise ValueError("command must not be empty")

    timeout_seconds = _validate_command_timeout(timeout_seconds)
    target_directory = _resolve_workspace_path(directory)

    if not await asyncio.to_thread(target_directory.exists):
        raise ValueError(f"Directory does not exist: {directory}")

    if not await asyncio.to_thread(target_directory.is_dir):
        raise ValueError(f"Path is not a directory: {directory}")

    shell = _get_command_shell()

    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(target_directory),
        executable=shell,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    timed_out = False

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        process.kill()
        stdout_bytes, stderr_bytes = await process.communicate()

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    combined_output, combined_truncated = _truncate_output(stdout + stderr)
    stdout, stdout_truncated = _truncate_output(stdout)
    stderr, stderr_truncated = _truncate_output(stderr)

    logger.info(f"Executed command in {_display_path(target_directory)} with exit code {process.returncode}: {command}")

    return {
        "command": command,
        "directory": _display_path(target_directory),
        "shell": shell,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "combined_output": combined_output,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "combined_output_truncated": combined_truncated,
    }


@mcp.tool
async def manage_agent_task(
    action: Literal["list", "get", "create", "update", "delete"],
    task_id: str | None = None,
    name: str | None = None,
    prompt: str | None = None,
    schedule_type: Literal["cron", "delayed"] | None = None,
    cron_expression: str | None = None,
    delay_seconds: int | None = None,
    enabled: bool | None = None,
    repeat: bool | None = None,
) -> dict[str, Any]:
    """
    Creates, lists, reads, updates, and deletes scheduled background AI tasks.

    Name is required for create and update actions.
    Prompt and schedule parameters are also required for create, while update allows partial updates of these fields.
    Supported schedule_type values are cron and delayed.
    Cron tasks use cron_expression.
    Delayed tasks use delay_seconds. They are one-shot by default and only repeat when repeat=true is explicitly set.
    """

    normalized_action = action.strip().lower()

    if normalized_action == "list":
        return await task_scheduler.list_tasks()

    if normalized_action == "get":
        if not task_id:
            raise ValueError("task_id is required for get")
        return await task_scheduler.get_task(task_id)

    if normalized_action == "create":
        if not name or not name.strip():
            raise ValueError("name is required for create")
        if not prompt or not prompt.strip():
            raise ValueError("prompt is required for create")
        if not schedule_type or not schedule_type.strip():
            raise ValueError("schedule_type is required for create")

        return await task_scheduler.create_task(
            name=name,
            prompt=prompt,
            schedule_type=schedule_type,
            cron_expression=cron_expression,
            delay_seconds=delay_seconds,
            timezone_name=settings.TIMEZONE,
            enabled=enabled if enabled is not None else True,
            repeat=repeat if repeat is not None else False,
        )

    if normalized_action == "update":
        if not task_id:
            raise ValueError("task_id is required for update")

        return await task_scheduler.update_task(
            task_id,
            name=name,
            prompt=prompt,
            schedule_type=schedule_type,
            cron_expression=cron_expression,
            delay_seconds=delay_seconds,
            timezone_name=settings.TIMEZONE,
            enabled=enabled,
            repeat=repeat,
        )

    if normalized_action == "delete":
        if not task_id:
            raise ValueError("task_id is required for delete")
        return await task_scheduler.delete_task(task_id)

    raise ValueError(f"Unsupported action: {action}")


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


@mcp_app.get(f"{settings.mcp_shortener_route_prefix()}/{{token}}", include_in_schema=False)
async def redirect_short_url(token: str) -> RedirectResponse:
    original_url = await local_url_shortener.resolve(token)
    if original_url is None:
        raise HTTPException(status_code=404, detail="Short URL not found")

    return RedirectResponse(url=original_url, status_code=307)


mcp_app.mount("/", _mcp_http_app)


def create_mcp_server() -> uvicorn.Server:
    return uvicorn.Server(
        uvicorn.Config(
            mcp_app,
            host=settings.MCP_HOST,
            port=settings.MCP_PORT,
            reload=False,
            log_config=uvicorn_log_config,
        )
    )


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


if __name__ == "__main__":
    asyncio.run(serve_mcp_server())
