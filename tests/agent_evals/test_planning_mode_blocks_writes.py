"""Adversarial eval: planning mode actually blocks mutations (plan P3 #20).

Scenario: the agent enters planning mode to think before acting on an ambiguous
request. Mutating tools must return `denied` until the agent calls
`exit_planning_mode` with a plan artifact. After exit, the same tool call succeeds.

This exercises the safety floor raised by P2 #11. If the planning gate regresses,
this test fails for the right reason.
"""

from __future__ import annotations

import pytest

from app import mcp as mcp_mod
from app.core.config import settings
from app.runtime import session_binding

_SESSION_KEY = "cli:planning-eval:user"
_MCP_SESSION_ID = "mcp-planning-eval"


class _Ctx:
    session_id = _MCP_SESSION_ID


@pytest.fixture
def bound_session(agent_eval_workspace):
    session_binding.bind_mcp_session_to_runtime_session(_MCP_SESSION_ID, _SESSION_KEY)
    yield


async def test_planning_mode_blocks_write_then_allows_after_exit(bound_session):
    # 1. Agent enters planning mode for a high-impact task.
    enter_result = await mcp_mod.enter_planning_mode(
        objective="Reorganize the workspace skills directory",
        ctx=_Ctx(),
    )
    assert enter_result["status"] == "ok"
    assert enter_result["mode"] == "planning"

    # 2. Agent attempts to write a file while still in planning mode — gate denies it.
    blocked = await mcp_mod.write_new_file("plans-output.txt", "draft body", ctx=_Ctx())
    assert blocked["status"] == "error"
    assert blocked["type"] == "denied"
    assert blocked["details"]["reason"] == "planning_mode_blocked"

    # Verify no file was created behind the gate.
    assert not (settings.WORKSPACE_ROOT / "plans-output.txt").exists()

    # 3. Agent exits planning mode with a plan summary; the artifact is recorded.
    exit_result = await mcp_mod.exit_planning_mode(
        plan_summary="Step 1: list current skills. Step 2: regroup by domain.",
        ctx=_Ctx(),
    )
    assert exit_result["status"] == "ok"
    assert exit_result["mode"] == "normal"
    plan_path = settings.WORKSPACE_ROOT / exit_result["plan_path"]
    assert plan_path.is_file()
    assert "Step 1" in plan_path.read_text(encoding="utf-8")

    # 4. Same mutating call now succeeds.
    allowed = await mcp_mod.write_new_file("plans-output.txt", "post-plan body", ctx=_Ctx())
    assert allowed.get("status") != "error", allowed
    assert (settings.WORKSPACE_ROOT / "plans-output.txt").read_text(encoding="utf-8") == "post-plan body"


async def test_planning_mode_keeps_read_only_tools_available(bound_session):
    """Counter-test: planning mode must not block read-only inspection that the model
    needs for context-gathering. If read_file were also blocked, planning mode would be
    useless."""
    (settings.WORKSPACE_ROOT / "context.txt").write_text("existing context\n", encoding="utf-8")

    await mcp_mod.enter_planning_mode(objective="Investigate before acting", ctx=_Ctx())

    read_result = await mcp_mod.read_file("context.txt", ctx=_Ctx())
    assert read_result.get("status") != "error", read_result
    assert read_result["content"] == "existing context\n"
