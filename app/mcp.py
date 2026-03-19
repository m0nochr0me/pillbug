"""
Composition MCP Server
"""

import asyncio
import hashlib
import mimetypes
import os
import re
from collections.abc import Callable
from contextlib import suppress
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import aiohttp
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
from app.util.compaction import apply_compaction_stages

__all__ = ("create_mcp_server", "mcp", "mcp_app")

mcp = FastMCP(f"{__project__}-composition-server")

_TODO_LIST_STATE_KEY = "todo_list"

_FETCH_URL_FILENAME_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")
_FETCH_URL_POSITIVE_HINT_PATTERN = re.compile(
    r"\b(article|body|content|entry|main|page|post|prose|story|text)\b",
    flags=re.IGNORECASE,
)
_FETCH_URL_NEGATIVE_HINT_PATTERN = re.compile(
    r"\b(ad|ads|aside|banner|breadcrumb|comment|cookie|footer|header|menu|modal|nav|related|share|sidebar|"
    r"social|subscribe|toolbar)\b",
    flags=re.IGNORECASE,
)


class _ReadableHtmlParser(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {
            "article",
            "blockquote",
            "dd",
            "div",
            "dl",
            "dt",
            "figcaption",
            "figure",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "hr",
            "li",
            "main",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "tr",
            "ul",
        }
    )
    _SKIP_TAGS = frozenset({"canvas", "iframe", "noscript", "script", "style", "svg", "template"})
    _NEGATIVE_TAGS = frozenset({"aside", "button", "dialog", "footer", "form", "header", "menu", "nav"})
    _POSITIVE_TAGS = frozenset({"article", "main"})

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._stack: list[tuple[str, bool, bool, bool]] = []
        self._skip_depth = 0
        self._negative_depth = 0
        self._positive_depth = 0
        self._body_fragments: list[str] = []
        self._focused_fragments: list[str] = []
        self._title_fragments: list[str] = []
        self._in_title = False
        self._link_stack: list[tuple[str | None, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.lower()
        attr_map = {name.lower(): value or "" for name, value in attrs}

        if normalized_tag == "title":
            self._in_title = True

        attr_text = " ".join(
            part
            for part in (attr_map.get("class"), attr_map.get("id"), attr_map.get("role"), attr_map.get("aria-label"))
            if part
        )
        positive = normalized_tag in self._POSITIVE_TAGS or attr_map.get("role", "").strip().lower() == "main"
        positive = positive or bool(_FETCH_URL_POSITIVE_HINT_PATTERN.search(attr_text))
        negative = normalized_tag in self._NEGATIVE_TAGS or bool(_FETCH_URL_NEGATIVE_HINT_PATTERN.search(attr_text))
        skip = normalized_tag in self._SKIP_TAGS
        positive = positive and not negative

        self._stack.append((normalized_tag, positive, negative, skip))

        if skip:
            self._skip_depth += 1
        if negative:
            self._negative_depth += 1
        if positive:
            self._positive_depth += 1

        if normalized_tag == "a":
            href = attr_map.get("href", "").strip() or None
            self._link_stack.append((href, []))
            return

        if normalized_tag == "img":
            alt_text = re.sub(r"\s+", " ", attr_map.get("alt", "")).strip()
            if alt_text:
                self._append_text(f"[Image: {alt_text}]")
            return

        if normalized_tag == "br":
            self._append_break()
        elif normalized_tag == "li":
            self._append_break(prefix="- ")

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()

        if normalized_tag == "title":
            self._in_title = False

        if normalized_tag == "a" and self._link_stack:
            href, link_text_parts = self._link_stack.pop()
            link_text = self._normalize_inline_text("".join(link_text_parts))
            resolved_href = urljoin(self._base_url, href) if href else ""

            if resolved_href and link_text and resolved_href != link_text:
                self._append_text(f"{link_text} ({resolved_href})")
            elif link_text:
                self._append_text(link_text)
            elif resolved_href:
                self._append_text(resolved_href)

        if normalized_tag in self._BLOCK_TAGS:
            self._append_break()

        self._pop_stack(normalized_tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_fragments.append(data)
            return

        normalized = self._normalize_inline_text(data)
        if not normalized or self._skip_depth:
            return

        if self._link_stack:
            self._link_stack[-1][1].append(f"{normalized} ")
            return

        self._append_text(normalized)

    def render(self) -> tuple[str | None, str]:
        body_text = self._normalize_output("".join(self._body_fragments))
        focused_text = self._normalize_output("".join(self._focused_fragments))
        title = self._normalize_output("".join(self._title_fragments)) or None

        if len(focused_text) >= max(400, len(body_text) // 5):
            return title, focused_text

        return title, body_text

    def _append_break(self, prefix: str = "") -> None:
        if self._skip_depth or self._negative_depth:
            return

        for target in self._targets():
            target.append("\n\n")
            if prefix:
                target.append(prefix)

    def _append_text(self, text: str) -> None:
        normalized = self._normalize_inline_text(text)
        if not normalized or self._skip_depth or self._negative_depth:
            return

        for target in self._targets():
            target.append(f"{normalized} ")

    def _targets(self) -> tuple[list[str], ...]:
        if self._positive_depth:
            return self._body_fragments, self._focused_fragments
        return (self._body_fragments,)

    def _pop_stack(self, tag: str) -> None:
        while self._stack:
            stack_tag, positive, negative, skip = self._stack.pop()
            if skip:
                self._skip_depth = max(self._skip_depth - 1, 0)
            if negative:
                self._negative_depth = max(self._negative_depth - 1, 0)
            if positive:
                self._positive_depth = max(self._positive_depth - 1, 0)
            if stack_tag == tag:
                return

    @staticmethod
    def _normalize_inline_text(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _normalize_output(text: str) -> str:
        compacted = re.sub(r"[ \t]+\n", "\n", text)
        compacted = re.sub(r"\n[ \t]+", "\n", compacted)
        compacted = re.sub(r"[ \t]{2,}", " ", compacted)
        compacted = re.sub(r"\s+([,.;:!?])", r"\1", compacted)
        compacted = re.sub(r"\n{3,}", "\n\n", compacted)
        return compacted.strip()


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


def _validate_fetch_url_max_bytes(max_bytes: int) -> int:
    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")

    return min(max_bytes, settings.MCP_FETCH_URL_MAX_BYTES)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write_text(path: Path, content: str, *, mode: str) -> int:
    with path.open(mode, encoding="utf-8") as file:
        return file.write(content)


def _write_bytes(path: Path, content: bytes) -> int:
    return path.write_bytes(content)


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


def _sanitize_fetch_url_filename(value: str, fallback: str) -> str:
    normalized = _FETCH_URL_FILENAME_SAFE_PATTERN.sub("-", value.strip().lower()).strip("-._")
    return normalized or fallback


def _looks_like_html(content_type: str, url: str) -> bool:
    normalized_content_type = content_type.lower()
    if normalized_content_type in {"application/xhtml+xml", "text/html"}:
        return True

    return Path(urlparse(url).path).suffix.lower() in {".htm", ".html", ".xhtml"}


def _looks_like_text(content_type: str, url: str) -> bool:
    normalized_content_type = content_type.lower()
    if normalized_content_type.startswith("text/"):
        return True

    if normalized_content_type in {
        "application/javascript",
        "application/json",
        "application/ld+json",
        "application/sql",
        "application/xml",
        "application/x-yaml",
        "application/yaml",
        "image/svg+xml",
    }:
        return True

    return Path(urlparse(url).path).suffix.lower() in {
        ".css",
        ".csv",
        ".js",
        ".json",
        ".md",
        ".rst",
        ".svg",
        ".toml",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }


def _decode_text_payload(payload: bytes, charset: str | None) -> str:
    encodings = [charset, "utf-8", "utf-16", "latin-1"]
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue

    return payload.decode("utf-8", errors="replace")


def _guess_fetch_url_extension(content_type: str, url: str, *, readable_html: bool) -> str:
    if readable_html:
        return ".md"

    guessed_extension = mimetypes.guess_extension(content_type.lower(), strict=False)
    if guessed_extension:
        return guessed_extension

    path_extension = Path(urlparse(url).path).suffix.lower()
    if path_extension:
        return path_extension

    if _looks_like_text(content_type, url):
        return ".txt"

    return ".bin"


def _build_fetch_url_output_path(url: str, content_type: str, *, readable_html: bool) -> Path:
    parsed_url = urlparse(url)
    host = _sanitize_fetch_url_filename(parsed_url.netloc or parsed_url.hostname or "resource", "resource")
    stem = _sanitize_fetch_url_filename(Path(parsed_url.path).stem or "index", "index")
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    extension = _guess_fetch_url_extension(content_type, url, readable_html=readable_html)
    output_dir = _resolve_workspace_path(settings.MCP_FETCH_URL_OUTPUT_DIR)
    return output_dir / f"{host}-{stem}-{digest}{extension}"


def _render_readable_html_document(title: str | None, source_url: str, body: str) -> str:
    heading = title.strip() if title else "Web Page"
    normalized_body = body.strip()
    duplicate_heading_prefix = f"{heading}\n\n"
    if normalized_body == heading:
        normalized_body = ""
    elif normalized_body.startswith(duplicate_heading_prefix):
        normalized_body = normalized_body.removeprefix(duplicate_heading_prefix).lstrip()

    lines = [f"# {heading}", "", f"Source: {source_url}", "", normalized_body]
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


async def _extract_readable_html(payload: bytes, final_url: str, charset: str | None) -> tuple[str | None, str]:
    html_text = _decode_text_payload(payload, charset)
    parser = _ReadableHtmlParser(final_url)
    parser.feed(html_text)
    parser.close()

    title, readable_text = parser.render()
    if readable_text:
        readable_text = await apply_compaction_stages(readable_text, ("url_shorten",))
        return title, readable_text

    fallback_text = re.sub(r"<[^>]+>", " ", html_text)
    fallback_text = re.sub(r"\s+", " ", fallback_text).strip()
    fallback_text = await apply_compaction_stages(fallback_text, ("url_shorten",))
    return title, fallback_text


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
async def fetch_url(
    url: str,
    output_path: str | None = None,
    max_bytes: int = settings.MCP_FETCH_URL_MAX_BYTES,
    timeout_seconds: float = settings.MCP_FETCH_URL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """
    Fetches a remote resource with aiohttp, saves it into the workspace, and returns the saved file path.

    HTML responses are converted into a reduced reading-mode markdown document before saving.
    """

    normalized_url = url.strip()
    if not normalized_url:
        raise ValueError("url must not be empty")

    max_bytes = _validate_fetch_url_max_bytes(max_bytes)
    timeout_seconds = _validate_command_timeout(timeout_seconds)

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": f"{__project__}/{__version__}",
    }

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        try:
            async with session.get(normalized_url, allow_redirects=True) as response:
                response.raise_for_status()

                if response.content_length is not None and response.content_length > max_bytes:
                    raise ValueError(
                        f"Resource size {response.content_length} bytes exceeds the configured limit of {max_bytes} bytes"
                    )

                payload = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        raise ValueError(
                            f"Resource exceeded the configured limit of {max_bytes} bytes while downloading"
                        )

                final_url = str(response.url)
                content_type = response.content_type.lower() if response.content_type else "application/octet-stream"
                charset = response.charset
                status_code = response.status
        except aiohttp.ClientError as exc:
            raise ValueError(f"Unable to fetch URL: {exc}") from exc

    shortened_urls = await local_url_shortener.shorten_many((normalized_url, final_url))
    readable_html = _looks_like_html(content_type, final_url)

    if output_path is not None:
        target_file = _resolve_workspace_path(output_path)
        if await asyncio.to_thread(target_file.exists) and not await asyncio.to_thread(target_file.is_file):
            raise ValueError(f"Path is not a file: {output_path}")
    else:
        target_file = _build_fetch_url_output_path(final_url, content_type, readable_html=readable_html)

    await asyncio.to_thread(target_file.parent.mkdir, parents=True, exist_ok=True)

    if readable_html:
        title, readable_text = await _extract_readable_html(bytes(payload), final_url, charset)
        stored_content = _render_readable_html_document(
            title,
            shortened_urls.get(final_url, final_url),
            readable_text,
        )
        stored_bytes = len(stored_content.encode("utf-8"))
        await asyncio.to_thread(_write_text, target_file, stored_content, mode="w")
        content_mode = "readable-html"
    elif _looks_like_text(content_type, final_url):
        text_content = _decode_text_payload(bytes(payload), charset)
        stored_bytes = len(text_content.encode("utf-8"))
        await asyncio.to_thread(_write_text, target_file, text_content, mode="w")
        content_mode = "text"
    else:
        stored_bytes = await asyncio.to_thread(_write_bytes, target_file, bytes(payload))
        content_mode = "binary"

    logger.info(f"Fetched URL {normalized_url} into {_display_path(target_file)}")

    return {
        "url": normalized_url,
        "short_url": shortened_urls.get(normalized_url, normalized_url),
        "final_url": final_url,
        "final_short_url": shortened_urls.get(final_url, final_url),
        "path": _display_path(target_file),
        "content_type": content_type,
        "content_mode": content_mode,
        "status_code": status_code,
        "bytes_downloaded": len(payload),
        "bytes_saved": stored_bytes,
        "max_bytes": max_bytes,
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
