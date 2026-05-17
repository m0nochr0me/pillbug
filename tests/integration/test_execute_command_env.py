"""execute_command subprocess environment scrub (plan P0 #1).

The subprocess environment must be built from an explicit allowlist instead of
inheriting the parent process env, so PB_GEMINI_API_KEY / PB_*_BEARER_TOKEN and
any other sensitive vars cannot be exfiltrated by a `printenv` invocation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import mcp as mcp_mod
from app.core.config import settings


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    return settings


def test_pillbug_secrets_are_not_inherited(monkeypatch, workspace_settings):
    monkeypatch.setenv("PB_GEMINI_API_KEY", "leak-me-please")
    monkeypatch.setenv("PB_DASHBOARD_BEARER_TOKEN", "another-leak-please")
    monkeypatch.setenv("PB_A2A_BEARER_TOKEN", "and-yet-another-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "definitely-not-allowed")

    env = mcp_mod._build_command_environment(ctx=None)

    assert "PB_GEMINI_API_KEY" not in env
    assert "PB_DASHBOARD_BEARER_TOKEN" not in env
    assert "PB_A2A_BEARER_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_pillbug_injected_vars_are_present(workspace_settings):
    env = mcp_mod._build_command_environment(ctx=None)

    assert env["PB_RUNTIME_ID"] == settings.runtime_id
    assert env["PB_WORKSPACE_ROOT"] == str(settings.WORKSPACE_ROOT)


def test_default_allowlist_vars_pass_through(monkeypatch, workspace_settings):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setenv("TZ", "UTC")

    env = mcp_mod._build_command_environment(ctx=None)

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/tmp/fake-home"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_ALL"] == "en_US.UTF-8"
    assert env["TERM"] == "xterm"
    assert env["TZ"] == "UTC"


def test_passthrough_can_forward_extra_names(monkeypatch, workspace_settings):
    monkeypatch.setattr(settings, "EXECUTE_COMMAND_ENV_PASSTHROUGH", "MY_TOOL_HOME,CUSTOM_VAR")
    monkeypatch.setenv("MY_TOOL_HOME", "/opt/my-tool")
    monkeypatch.setenv("CUSTOM_VAR", "yes")
    monkeypatch.setenv("UNRELATED_VAR", "no")

    env = mcp_mod._build_command_environment(ctx=None)

    assert env["MY_TOOL_HOME"] == "/opt/my-tool"
    assert env["CUSTOM_VAR"] == "yes"
    assert "UNRELATED_VAR" not in env


def test_passthrough_cannot_unblock_sensitive_names(monkeypatch, workspace_settings):
    monkeypatch.setattr(
        settings,
        "EXECUTE_COMMAND_ENV_PASSTHROUGH",
        "MY_SECRET_TOKEN,SOME_PASSWORD,API_KEY",
    )
    monkeypatch.setenv("MY_SECRET_TOKEN", "still-blocked")
    monkeypatch.setenv("SOME_PASSWORD", "still-blocked")
    monkeypatch.setenv("API_KEY", "still-blocked")

    env = mcp_mod._build_command_environment(ctx=None)

    assert "MY_SECRET_TOKEN" not in env
    assert "SOME_PASSWORD" not in env
    assert "API_KEY" not in env


def test_pb_public_prefix_overrides_sensitive_block(monkeypatch, workspace_settings):
    monkeypatch.setattr(settings, "EXECUTE_COMMAND_ENV_PASSTHROUGH", "PB_PUBLIC_DEMO_TOKEN")
    monkeypatch.setenv("PB_PUBLIC_DEMO_TOKEN", "intentionally-shared")

    env = mcp_mod._build_command_environment(ctx=None)

    assert env["PB_PUBLIC_DEMO_TOKEN"] == "intentionally-shared"


async def test_execute_command_printenv_does_not_leak_secrets(monkeypatch, workspace_settings):
    monkeypatch.setenv("PB_GEMINI_API_KEY", "leak-me-please-via-subprocess")
    monkeypatch.setenv("PB_DASHBOARD_BEARER_TOKEN", "leak-me-too")
    monkeypatch.setattr(settings, "EXECUTE_COMMAND_ALLOWLIST", r"^printenv( \| sort)?$")

    result = await mcp_mod.execute_command("printenv | sort", directory=".", ctx=None)

    stdout = result["stdout"]
    assert "leak-me-please-via-subprocess" not in stdout
    assert "leak-me-too" not in stdout
    assert "PB_GEMINI_API_KEY" not in stdout
    assert "PB_DASHBOARD_BEARER_TOKEN" not in stdout
    assert f"PB_RUNTIME_ID={settings.runtime_id}" in stdout
    assert f"PB_WORKSPACE_ROOT={settings.WORKSPACE_ROOT}" in stdout
