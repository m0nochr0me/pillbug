"""
Composition MCP Server.

Package layout: FastMCP/FastAPI instances and bootstrap in server.py, bearer
auth in auth.py, shared path/validator helpers in shared.py, built-in tools
under tools/, and HTTP routes under http/.
"""

# Re-exported tool objects, dispatch helper, and the aiohttp module: tests and
# external callers reach them as attributes of `app.mcp` (e.g.
# mcp_mod.execute_command, monkeypatch on mcp_mod.aiohttp). Keep this surface
# stable.
import aiohttp as aiohttp

# Route modules register handlers on mcp_app at import time; the catch-all MCP
# mount below must stay after these imports so custom routes keep precedence.
from app.mcp.http import a2a, control, shortener, telemetry  # noqa: F401
from app.mcp.server import (
    _mcp_http_app,
    bind_application_loop,
    create_mcp_server,
    mcp,
    mcp_app,
    serve_mcp_server,
    wait_for_server_startup,
)
from app.mcp.tools.commands import draft_command as draft_command
from app.mcp.tools.commands import execute_command as execute_command
from app.mcp.tools.commands import run_approved_command as run_approved_command
from app.mcp.tools.fetch import fetch_url as fetch_url
from app.mcp.tools.files import find_files as find_files
from app.mcp.tools.files import list_files as list_files
from app.mcp.tools.files import read_file as read_file
from app.mcp.tools.files import replace_file_text as replace_file_text
from app.mcp.tools.files import search_file_regex as search_file_regex
from app.mcp.tools.files import write_new_file as write_new_file
from app.mcp.tools.outbound import commit_outbound_message as commit_outbound_message
from app.mcp.tools.outbound import draft_outbound_message as draft_outbound_message
from app.mcp.tools.outbound import list_a2a_peers as list_a2a_peers
from app.mcp.tools.outbound import request_a2a_response as request_a2a_response
from app.mcp.tools.outbound import send_a2a_message as send_a2a_message
from app.mcp.tools.outbound import send_file as send_file
from app.mcp.tools.outbound import send_message as send_message
from app.mcp.tools.planning import enter_planning_mode as enter_planning_mode
from app.mcp.tools.planning import exit_planning_mode as exit_planning_mode
from app.mcp.tools.runtime_info import get_runtime_info as get_runtime_info
from app.mcp.tools.tasks import manage_agent_task as manage_agent_task
from app.mcp.tools.todo import manage_todo_list as manage_todo_list
from app.runtime.outbound_dispatch import (  # noqa: F401
    dispatch_outbound_draft as _dispatch_outbound_draft,
)

__all__ = (
    "bind_application_loop",
    "create_mcp_server",
    "mcp",
    "mcp_app",
    "serve_mcp_server",
    "wait_for_server_startup",
)

mcp_app.mount("/", _mcp_http_app)
