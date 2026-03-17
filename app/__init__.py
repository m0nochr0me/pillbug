"""
Pillbug is an AI agent operating system designed to manage and orchestrate multiple AI agents, enabling them to work together seamlessly. It provides a framework for creating, deploying, and managing AI agents, allowing them to communicate and collaborate effectively to achieve complex tasks.
"""

import os

# FastMCP installs its own stderr logger during import. Keep those records in the
# application logging pipeline so CLI mode is not polluted with tool output.
os.environ.setdefault("FASTMCP_LOG_ENABLED", "false")

__version__ = "0.1.0"
__project__ = "pillbug"

__banner__ = f"""
           ███  ████  ████  █████  v{__version__}
                 ███   ███   ███
████████  ████   ███   ███   ███████  █████ ████  ███████
 ███  ███  ███   ███   ███   ███  ███  ███  ███  ███  ███
 ███  ███  ███   ███   ███   ███  ███  ███  ███  ███  ███
 ███  ███  ███   ███   ███   ███  ███  ███  ███  ███  ███
 ███████  █████ █████ █████ ████████    ████████  ███████
 ███                                                  ███
 ███                                             ███  ███
█████                                             ██████
"""

