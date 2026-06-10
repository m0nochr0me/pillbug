"""
Shell command execution.

Runtime-layer home for the validated subprocess runner shared by the MCP command tools
(`app.mcp`) and the runtime loop's `/yes` command-approval flow. The MCP layer imports
from here; the runtime must never import from `app.mcp`.
"""

import asyncio
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from fastmcp import Context

from app.core.config import settings
from app.core.log import logger
from app.runtime.session_binding import get_runtime_session_for_mcp_session
from app.util.text import classify_shell_stderr
from app.util.tool_result import tool_error
from app.util.workspace import display_path, resolve_path_within_root, truncate_text

_DEFAULT_COMMAND_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TERM",
        "TZ",
    }
)

_DEFAULT_COMMAND_ENV_ALLOWLIST_PATTERNS: tuple[str, ...] = ("LC_*",)

_SENSITIVE_ENV_NAME_PATTERN = re.compile(r"(token|secret|key|password|credential)", re.IGNORECASE)
_SENSITIVE_OVERRIDE_PREFIX = "PB_PUBLIC_"


def _display_path(path: Path) -> str:
    return display_path(path, settings.WORKSPACE_ROOT)


def _resolve_workspace_path(path: str | Path) -> Path:
    return resolve_path_within_root(path, settings.WORKSPACE_ROOT)


def validate_command_timeout(timeout_seconds: float) -> float:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0")

    return min(timeout_seconds, settings.MCP_MAX_COMMAND_TIMEOUT_SECONDS)


def get_command_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell and Path(shell).is_file():
        return shell

    return "/bin/sh"


def _env_name_is_sensitive(name: str) -> bool:
    if name.startswith(_SENSITIVE_OVERRIDE_PREFIX):
        return False
    return bool(_SENSITIVE_ENV_NAME_PATTERN.search(name))


def _env_name_is_allowed(name: str, passthrough: frozenset[str]) -> bool:
    if name in _DEFAULT_COMMAND_ENV_ALLOWLIST:
        return True
    if name in passthrough:
        return True
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _DEFAULT_COMMAND_ENV_ALLOWLIST_PATTERNS)


def build_command_environment(ctx: Context | None) -> dict[str, str]:
    passthrough = frozenset(settings.execute_command_env_passthrough())
    environment: dict[str, str] = {}

    for env_name, env_value in os.environ.items():
        if not _env_name_is_allowed(env_name, passthrough):
            continue
        if _env_name_is_sensitive(env_name):
            continue
        environment[env_name] = env_value

    environment["PB_RUNTIME_ID"] = settings.runtime_id
    environment["PB_WORKSPACE_ROOT"] = str(settings.WORKSPACE_ROOT)

    if ctx is not None:
        runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
        if runtime_session_key:
            environment["PB_SESSION_KEY"] = runtime_session_key
            environment["PB_SESSION_KEY_SAFE"] = runtime_session_key.replace(":", "__")

    return environment


async def run_shell_command(
    command: str,
    *,
    directory: str,
    timeout_seconds: float,
    ctx: Context | None,
) -> dict[str, Any]:
    """Spawn the validated command; returns the structured result dict or an envelope on input errors."""

    timeout_seconds = validate_command_timeout(timeout_seconds)
    target_directory = _resolve_workspace_path(directory)

    if not await asyncio.to_thread(target_directory.exists):
        return tool_error("not_found", f"Directory does not exist: {directory}")

    if not await asyncio.to_thread(target_directory.is_dir):
        return tool_error("invalid_arguments", f"Path is not a directory: {directory}")

    shell = get_command_shell()
    environment = build_command_environment(ctx)

    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(target_directory),
        executable=shell,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
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
    shell_error = classify_shell_stderr(stderr)
    combined_output, combined_truncated = truncate_text(stdout + stderr, settings.MCP_MAX_COMMAND_OUTPUT_CHARS)
    stdout, stdout_truncated = truncate_text(stdout, settings.MCP_MAX_COMMAND_OUTPUT_CHARS)
    stderr, stderr_truncated = truncate_text(stderr, settings.MCP_MAX_COMMAND_OUTPUT_CHARS)

    exit_code = process.returncode
    if timed_out:
        run_status = "timeout"
    elif exit_code is None or exit_code == 0:
        run_status = "ok"
    elif exit_code < 0:
        run_status = "signal_terminated"
    else:
        run_status = "non_zero_exit"

    logger.info(f"Executed command in {_display_path(target_directory)} with exit code {exit_code}: {command}")

    return {
        "command": command,
        "directory": _display_path(target_directory),
        "shell": shell,
        "status": run_status,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "shell_error": shell_error,
        "stdout": stdout,
        "stderr": stderr,
        "combined_output": combined_output,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "combined_output_truncated": combined_truncated,
    }
