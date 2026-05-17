"""Shared fixtures for agent-behavior eval fixtures (plan P3 #20).

These tests treat the harness as the system under test, not the model. Each test
scripts a sequence of MCP tool calls that mimic what a Gemini model might propose
in response to an adversarial inbound message, then asserts the safety controls
defined by P0/P2 actually fired.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings
from app.core.telemetry import runtime_telemetry
from app.runtime import session_binding, task_runtime_state
from app.runtime.approvals import approval_store, outbound_draft_store
from app.runtime.outbound_budget import outbound_send_budget
from app.runtime.session_mode import (
    clear_session_mode,
    enter_planning_mode,
    exit_planning_mode,
)


@pytest.fixture
def agent_eval_workspace(isolated_settings, tmp_workspace: Path, monkeypatch):
    """Per-test isolation matching the agent-eval shape: clean stores + a real workspace."""
    monkeypatch.setattr(settings, "MCP_FETCH_URL_OUTPUT_DIR", settings.WORKSPACE_ROOT / "fetched", raising=True)

    approval_store._cache.clear()
    approval_store._loaded_dir = None
    outbound_draft_store._cache.clear()
    outbound_draft_store._loaded_dir = None
    session_binding._mcp_runtime_sessions.clear()
    session_binding._runtime_session_loaded_skills.clear()
    task_runtime_state._task_forbidden_actions.clear()
    outbound_send_budget.reset()
    runtime_telemetry._events.clear()

    # Reset planning-mode registry in case a prior test polluted it.
    for _session_key in list(session_binding._mcp_runtime_sessions.values()):
        clear_session_mode(_session_key)

    yield settings

    # Best-effort cleanup so a failing test never leaks planning state into the next.
    for session_key in (
        "cli:eval-conv:user",
        "cli:planning-eval:user",
    ):
        clear_session_mode(session_key)


# Re-export the planning-mode helpers so individual tests can keep imports minimal.
__all__ = ("agent_eval_workspace", "enter_planning_mode", "exit_planning_mode")
