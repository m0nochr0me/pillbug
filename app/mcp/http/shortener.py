"""Local short-URL redirect route."""

# Re-exported tool objects and the aiohttp module: tests and external callers
# reach them as attributes of `app.mcp` (e.g. mcp_mod.execute_command,
# monkeypatch on mcp_mod.aiohttp). Keep this surface stable.
import aiohttp as aiohttp  # noqa: E402
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from app.core.config import settings
from app.core.url_shortener import local_url_shortener
from app.mcp.server import (
    mcp_app,
)
from app.mcp.tools.commands import draft_command as draft_command  # noqa: E402
from app.mcp.tools.commands import execute_command as execute_command  # noqa: E402
from app.mcp.tools.commands import run_approved_command as run_approved_command  # noqa: E402
from app.mcp.tools.fetch import fetch_url as fetch_url  # noqa: E402
from app.mcp.tools.files import find_files as find_files  # noqa: E402
from app.mcp.tools.files import list_files as list_files  # noqa: E402
from app.mcp.tools.files import read_file as read_file  # noqa: E402
from app.mcp.tools.files import replace_file_text as replace_file_text  # noqa: E402
from app.mcp.tools.files import search_file_regex as search_file_regex  # noqa: E402
from app.mcp.tools.files import write_new_file as write_new_file  # noqa: E402
from app.mcp.tools.outbound import commit_outbound_message as commit_outbound_message  # noqa: E402
from app.mcp.tools.outbound import draft_outbound_message as draft_outbound_message  # noqa: E402
from app.mcp.tools.outbound import list_a2a_peers as list_a2a_peers  # noqa: E402
from app.mcp.tools.outbound import request_a2a_response as request_a2a_response  # noqa: E402
from app.mcp.tools.outbound import send_a2a_message as send_a2a_message  # noqa: E402
from app.mcp.tools.outbound import send_file as send_file  # noqa: E402
from app.mcp.tools.outbound import send_message as send_message  # noqa: E402
from app.mcp.tools.planning import enter_planning_mode as enter_planning_mode  # noqa: E402
from app.mcp.tools.planning import exit_planning_mode as exit_planning_mode  # noqa: E402
from app.mcp.tools.runtime_info import get_runtime_info as get_runtime_info  # noqa: E402
from app.mcp.tools.tasks import manage_agent_task as manage_agent_task  # noqa: E402
from app.mcp.tools.todo import manage_todo_list as manage_todo_list  # noqa: E402


@mcp_app.get(f"{settings.mcp_shortener_route_prefix()}/{{token}}", include_in_schema=False)
async def redirect_short_url(token: str) -> RedirectResponse:
    original_url = await local_url_shortener.resolve(token)
    if original_url is None:
        raise HTTPException(status_code=404, detail="Short URL not found")

    return RedirectResponse(url=original_url, status_code=307)
