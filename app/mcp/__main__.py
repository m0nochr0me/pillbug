"""Run the composition MCP server: `python -m app.mcp`."""

import asyncio

from app.mcp import serve_mcp_server

if __name__ == "__main__":
    asyncio.run(serve_mcp_server())
