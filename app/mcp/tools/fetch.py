"""URL fetching MCP tool with trust-banner provenance."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import aiohttp

from app import __project__, __version__
from app.core.config import settings
from app.core.log import logger
from app.core.url_shortener import local_url_shortener
from app.mcp.server import (
    mcp,
)
from app.mcp.shared import (
    _display_path,
    _resolve_workspace_path,
    _validate_fetch_url_max_bytes,
)
from app.runtime.command_execution import (
    validate_command_timeout as _validate_command_timeout,
)
from app.util.tool_result import envelope_error, tool_error
from app.util.web import (
    build_fetch_output_path,
    decode_text_payload,
    extract_readable_html,
    looks_like_html,
    looks_like_text,
    render_readable_html_document,
    render_trust_banner,
    render_trust_banner_metadata,
)
from app.util.workspace import (
    async_write_bytes_file,
    async_write_text_file,
)


@mcp.tool
@envelope_error
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
        return tool_error("invalid_arguments", "url must not be empty")

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
                    return tool_error(
                        "invalid_arguments",
                        f"Resource size {response.content_length} bytes exceeds the configured limit of {max_bytes} bytes",
                        details={"content_length": response.content_length, "max_bytes": max_bytes},
                    )

                payload = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        return tool_error(
                            "invalid_arguments",
                            f"Resource exceeded the configured limit of {max_bytes} bytes while downloading",
                            details={"max_bytes": max_bytes},
                        )

                final_url = str(response.url)
                content_type = response.content_type.lower() if response.content_type else "application/octet-stream"
                charset = response.charset
                status_code = response.status
        except aiohttp.ClientError as exc:
            return tool_error("internal_error", f"Unable to fetch URL: {exc}")

    shortened_urls = await local_url_shortener.shorten_many((normalized_url, final_url))
    readable_html = looks_like_html(content_type, final_url)

    if output_path is not None:
        target_file = _resolve_workspace_path(output_path)
        if await asyncio.to_thread(target_file.exists) and not await asyncio.to_thread(target_file.is_file):
            return tool_error("invalid_arguments", f"Path is not a file: {output_path}")
    else:
        target_file = build_fetch_output_path(
            final_url,
            content_type,
            _resolve_workspace_path(settings.MCP_FETCH_URL_OUTPUT_DIR),
            readable_html=readable_html,
        )

    await asyncio.to_thread(target_file.parent.mkdir, parents=True, exist_ok=True)

    fetched_at = datetime.now(tz=UTC)
    provenance_sidecar: str | None = None

    if readable_html:
        title, readable_text = await extract_readable_html(bytes(payload), final_url, charset)
        document = render_readable_html_document(
            title,
            shortened_urls.get(final_url, final_url),
            readable_text,
        )
        banner = render_trust_banner(
            source_url=normalized_url,
            final_url=final_url,
            fetched_at=fetched_at,
            content_type=content_type,
            content_mode="readable-html",
        )
        stored_content = banner + document
        stored_bytes = len(stored_content.encode("utf-8"))
        await async_write_text_file(target_file, stored_content, mode="w")
        content_mode = "readable-html"
    elif looks_like_text(content_type, final_url):
        text_content = decode_text_payload(bytes(payload), charset)
        banner = render_trust_banner(
            source_url=normalized_url,
            final_url=final_url,
            fetched_at=fetched_at,
            content_type=content_type,
            content_mode="text",
        )
        stored_content = banner + text_content
        stored_bytes = len(stored_content.encode("utf-8"))
        await async_write_text_file(target_file, stored_content, mode="w")
        content_mode = "text"
    else:
        stored_bytes = await async_write_bytes_file(target_file, bytes(payload))
        content_mode = "binary"
        sidecar_path = target_file.parent / f"{target_file.name}.metadata.json"
        sidecar_metadata = render_trust_banner_metadata(
            source_url=normalized_url,
            final_url=final_url,
            fetched_at=fetched_at,
            content_type=content_type,
            content_mode="binary",
        )
        await async_write_text_file(sidecar_path, json.dumps(sidecar_metadata, indent=2) + "\n", mode="w")
        provenance_sidecar = _display_path(sidecar_path)

    logger.info(f"Fetched URL {normalized_url} into {_display_path(target_file)}")

    result = {
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
    if provenance_sidecar is not None:
        result["provenance_sidecar"] = provenance_sidecar
    return result
