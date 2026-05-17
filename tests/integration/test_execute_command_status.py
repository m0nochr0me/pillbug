"""execute_command and run_approved_command return a status + shell_error taxonomy (plan P2 #14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import mcp as mcp_mod
from app.core.config import settings


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    return settings


async def test_status_ok_on_success(monkeypatch, workspace_settings):
    monkeypatch.setattr(settings, "EXECUTE_COMMAND_ALLOWLIST", r"^echo .*$")
    result = await mcp_mod.execute_command("echo hello", directory=".")
    assert result["status"] == "ok"
    assert result["exit_code"] == 0
    assert result["shell_error"] is None
    assert result["timed_out"] is False


async def test_status_non_zero_exit_with_command_not_found(monkeypatch, workspace_settings):
    monkeypatch.setattr(settings, "EXECUTE_COMMAND_ALLOWLIST", r"^pb-nope-not-a-real-cmd-xyzzy$")
    result = await mcp_mod.execute_command("pb-nope-not-a-real-cmd-xyzzy", directory=".")
    assert result["status"] == "non_zero_exit"
    assert result["exit_code"] != 0
    assert result["shell_error"] == "command_not_found"


async def test_status_timeout(monkeypatch, workspace_settings):
    monkeypatch.setattr(settings, "EXECUTE_COMMAND_ALLOWLIST", r"^sleep \d+$")
    result = await mcp_mod.execute_command("sleep 5", directory=".", timeout_seconds=0.2)
    assert result["status"] == "timeout"
    assert result["timed_out"] is True
