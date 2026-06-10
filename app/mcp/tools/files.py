"""Workspace file MCP tools: list, read, write, replace, search, find."""

import asyncio
import re
from typing import Any

from fastmcp import Context

from app.core.config import settings
from app.core.log import logger
from app.core.telemetry import runtime_telemetry
from app.mcp.server import (
    mcp,
)
from app.mcp.shared import (
    _display_path,
    _resolve_workspace_path,
    _validate_max_results,
    _validate_page_size,
)
from app.mcp.tools.planning import _enforce_planning_gate
from app.runtime.session_binding import (
    get_runtime_session_for_mcp_session,
    record_runtime_session_skill_load,
)
from app.util.skills import workspace_skill_name_for_path
from app.util.tool_result import envelope_error, tool_error
from app.util.web import (
    parse_trust_banner,
)
from app.util.workspace import (
    async_read_text_file,
    async_write_text_file,
    is_hidden_path,
)


@mcp.tool
@envelope_error
async def list_files(
    directory: str = ".",
    include_hidden: bool = False,
) -> dict[str, Any]:
    """
    Lists files and directories directly under a workspace-relative directory.
    """

    target_directory = _resolve_workspace_path(directory)

    if not await asyncio.to_thread(target_directory.exists):
        return tool_error("not_found", f"Directory does not exist: {directory}")

    if not await asyncio.to_thread(target_directory.is_dir):
        return tool_error("invalid_arguments", f"Path is not a directory: {directory}")

    def build_entries() -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []

        for entry in sorted(target_directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            relative_path = entry.relative_to(settings.WORKSPACE_ROOT)
            if not include_hidden and is_hidden_path(relative_path):
                continue

            entry_type = "directory" if entry.is_dir() else "file"
            entries.append(
                {
                    "name": entry.name,
                    "path": _display_path(entry),
                    "type": entry_type,
                    "size": entry.stat().st_size if entry.is_file() else None,
                },
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
@envelope_error
async def read_file(
    path: str,
    start_line: int = 1,
    page_size: int = settings.MCP_DEFAULT_PAGE_SIZE,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Reads a UTF-8 text file from the workspace with line-based pagination.
    """

    if start_line < 1:
        return tool_error("invalid_arguments", "start_line must be at least 1")

    page_size = _validate_page_size(page_size)
    target_file = _resolve_workspace_path(path)

    if not await asyncio.to_thread(target_file.exists):
        return tool_error(
            "not_found",
            f"File does not exist: {path}",
            next_valid_actions=("find_files", "list_files"),
        )

    if not await asyncio.to_thread(target_file.is_file):
        return tool_error("invalid_arguments", f"Path is not a file: {path}")

    content = await async_read_text_file(target_file)
    provenance = parse_trust_banner(content)
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    start_index = min(start_line - 1, total_lines)
    page_lines = lines[start_index : start_index + page_size]
    end_line = start_index + len(page_lines)

    logger.debug(f"Read {len(page_lines)} lines from {_display_path(target_file)} starting at line {start_line}")

    # P1 #9 hook: when the model reads a SKILL.md, record the load so the rehydration
    # bundle can remind it which skills are already in context after a compress.
    # P2 #18: emit a one-shot `skill.loaded` telemetry event so operators can see hot skills.
    if ctx is not None:
        skill_name = workspace_skill_name_for_path(target_file)
        if skill_name is not None:
            runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
            if runtime_session_key:
                newly_loaded = record_runtime_session_skill_load(runtime_session_key, skill_name)
                if newly_loaded:
                    await runtime_telemetry.record_event(
                        event_type="skill.loaded",
                        source="mcp",
                        level="info",
                        message=f"skill loaded: {skill_name}",
                        data={
                            "skill_name": skill_name,
                            "runtime_session_key": runtime_session_key,
                        },
                    )

    result: dict[str, Any] = {
        "path": _display_path(target_file),
        "start_line": start_line,
        "end_line": end_line,
        "page_size": page_size,
        "total_lines": total_lines,
        "has_more": end_line < total_lines,
        "content": "".join(page_lines),
    }
    if provenance is not None:
        result["provenance"] = provenance[0]
    return result


@mcp.tool
@envelope_error
async def write_new_file(
    path: str,
    content: str,
    make_parents: bool = True,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Creates a new UTF-8 text file in the workspace and fails if it already exists.
    """

    if blocked := _enforce_planning_gate("write_new_file", ctx):
        return blocked

    target_file = _resolve_workspace_path(path)

    if await asyncio.to_thread(target_file.exists):
        return tool_error(
            "conflict",
            f"File already exists: {path}",
            next_valid_actions=("replace_file_text", "read_file"),
        )

    if make_parents:
        await asyncio.to_thread(target_file.parent.mkdir, parents=True, exist_ok=True)
    elif not await asyncio.to_thread(target_file.parent.exists):
        return tool_error(
            "not_found",
            f"Parent directory does not exist: {_display_path(target_file.parent)}",
        )

    chars_written = await async_write_text_file(target_file, content, mode="x")
    logger.info(f"Created file {_display_path(target_file)}")

    return {
        "path": _display_path(target_file),
        "chars_written": chars_written,
    }


@mcp.tool
@envelope_error
async def replace_file_text(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    expected_occurrences: int | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Replaces literal text inside an existing UTF-8 text file.
    """

    if blocked := _enforce_planning_gate("replace_file_text", ctx):
        return blocked

    if not old_text:
        return tool_error("invalid_arguments", "old_text must not be empty")

    target_file = _resolve_workspace_path(path)

    if not await asyncio.to_thread(target_file.exists):
        return tool_error("not_found", f"File does not exist: {path}")

    if not await asyncio.to_thread(target_file.is_file):
        return tool_error("invalid_arguments", f"Path is not a file: {path}")

    content = await async_read_text_file(target_file)
    occurrences = content.count(old_text)

    if occurrences == 0:
        return tool_error(
            "not_found",
            "old_text was not found in the file",
            next_valid_actions=("search_file_regex", "read_file"),
        )

    if expected_occurrences is not None and occurrences != expected_occurrences:
        return tool_error(
            "conflict",
            f"Expected {expected_occurrences} occurrences of old_text, but found {occurrences}",
            details={"occurrences_found": occurrences, "expected": expected_occurrences},
        )

    replacement_count = occurrences if replace_all else 1
    updated_content = content.replace(old_text, new_text, replacement_count)
    await async_write_text_file(target_file, updated_content, mode="w")
    logger.info(f"Replaced {replacement_count} occurrence(s) in {_display_path(target_file)}")

    return {
        "path": _display_path(target_file),
        "occurrences_found": occurrences,
        "occurrences_replaced": replacement_count,
    }


@mcp.tool
@envelope_error
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
        return tool_error("not_found", f"File does not exist: {path}")

    if not await asyncio.to_thread(target_file.is_file):
        return tool_error("invalid_arguments", f"Path is not a file: {path}")

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return tool_error("invalid_arguments", f"Invalid regular expression: {exc}")

    content = await async_read_text_file(target_file)
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
                },
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
@envelope_error
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
            if not include_hidden and is_hidden_path(relative_path):
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
