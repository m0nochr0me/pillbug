"""Each MCP tool surfaces structured envelopes for bad input, not raw raises (plan P0 #4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import mcp as mcp_mod
from app.core.config import settings


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    return settings


def _assert_envelope(result, *, expected_type: str):
    assert isinstance(result, dict), f"expected envelope dict, got {type(result).__name__}: {result!r}"
    assert result.get("status") == "error", result
    assert result.get("type") == expected_type, result
    assert "message" in result and isinstance(result["message"], str)
    assert "next_valid_actions" in result and isinstance(result["next_valid_actions"], list)
    assert "details" in result and isinstance(result["details"], dict)


async def test_read_file_missing_returns_not_found(workspace_settings):
    result = await mcp_mod.read_file("does/not/exist.txt")
    _assert_envelope(result, expected_type="not_found")
    assert "find_files" in result["next_valid_actions"]


async def test_read_file_path_is_directory_returns_invalid_arguments(workspace_settings):
    (settings.WORKSPACE_ROOT / "subdir").mkdir()
    result = await mcp_mod.read_file("subdir")
    _assert_envelope(result, expected_type="invalid_arguments")


async def test_read_file_path_traversal_is_invalid_arguments(workspace_settings):
    result = await mcp_mod.read_file("../escape.txt")
    _assert_envelope(result, expected_type="invalid_arguments")


async def test_write_new_file_conflict_returns_conflict(workspace_settings):
    target = settings.WORKSPACE_ROOT / "already.txt"
    target.write_text("existing", encoding="utf-8")
    result = await mcp_mod.write_new_file("already.txt", "new")
    _assert_envelope(result, expected_type="conflict")


async def test_replace_file_text_missing_old_text_returns_not_found(workspace_settings):
    target = settings.WORKSPACE_ROOT / "notes.txt"
    target.write_text("hello world", encoding="utf-8")
    result = await mcp_mod.replace_file_text("notes.txt", "missing-token", "x")
    _assert_envelope(result, expected_type="not_found")


async def test_replace_file_text_occurrence_mismatch_returns_conflict(workspace_settings):
    target = settings.WORKSPACE_ROOT / "notes.txt"
    target.write_text("hello hello", encoding="utf-8")
    result = await mcp_mod.replace_file_text("notes.txt", "hello", "x", expected_occurrences=3)
    _assert_envelope(result, expected_type="conflict")
    assert result["details"]["occurrences_found"] == 2


async def test_search_file_regex_invalid_pattern_returns_invalid_arguments(workspace_settings):
    target = settings.WORKSPACE_ROOT / "log.txt"
    target.write_text("data\n", encoding="utf-8")
    result = await mcp_mod.search_file_regex("log.txt", "([unterminated")
    _assert_envelope(result, expected_type="invalid_arguments")


async def test_list_files_missing_directory_returns_not_found(workspace_settings):
    result = await mcp_mod.list_files("not/a/dir")
    _assert_envelope(result, expected_type="not_found")


async def test_execute_command_empty_returns_invalid_arguments(workspace_settings):
    result = await mcp_mod.execute_command("   ", directory=".")
    _assert_envelope(result, expected_type="invalid_arguments")


async def test_execute_command_bad_directory_returns_not_found(monkeypatch, workspace_settings):
    monkeypatch.setattr(settings, "EXECUTE_COMMAND_ALLOWLIST", r"^echo .*$")
    result = await mcp_mod.execute_command("echo hi", directory="does/not/exist")
    _assert_envelope(result, expected_type="not_found")


async def test_send_message_empty_returns_invalid_arguments(workspace_settings):
    result = await mcp_mod.send_message("cli", "   ")
    _assert_envelope(result, expected_type="invalid_arguments")


async def test_send_message_unknown_channel_returns_not_found(workspace_settings):
    result = await mcp_mod.send_message("nonexistent_channel:abc", "hello")
    _assert_envelope(result, expected_type="not_found")


async def test_manage_todo_list_unsupported_action_returns_invalid_arguments(workspace_settings):
    # Build a minimal FastMCP context-like object; manage_todo_list reads action first.
    class _StubCtx:
        session_id = "test-session"

        async def get_state(self, _key):
            return None

    result = await mcp_mod.manage_todo_list(action="frobnicate", ctx=_StubCtx())  # type: ignore[arg-type]
    _assert_envelope(result, expected_type="invalid_arguments")


async def test_manage_agent_task_missing_task_id_returns_invalid_arguments(workspace_settings):
    result = await mcp_mod.manage_agent_task(action="get")
    _assert_envelope(result, expected_type="invalid_arguments")


async def test_fetch_url_empty_returns_invalid_arguments(workspace_settings):
    result = await mcp_mod.fetch_url("  ")
    _assert_envelope(result, expected_type="invalid_arguments")
