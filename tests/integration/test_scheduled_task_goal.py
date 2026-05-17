"""Scheduled-task goal contract (plan P2 #12)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import mcp as mcp_mod
from app.core.config import settings
from app.runtime import session_binding, task_runtime_state
from app.runtime.approvals import outbound_draft_store
from app.runtime.scheduler import AgentTaskScheduler
from app.schema.tasks import (
    AgentTaskDefinition,
    AgentTaskGoal,
    AgentTaskRunRecord,
    CronTaskSchedule,
    DelayedTaskSchedule,
)


@pytest.fixture
def workspace_settings(isolated_settings, tmp_workspace: Path):
    outbound_draft_store._cache.clear()
    outbound_draft_store._loaded_dir = None
    session_binding._mcp_runtime_sessions.clear()
    task_runtime_state._task_forbidden_actions.clear()
    return settings


def _delayed_definition(task_id: str = "task-12345") -> AgentTaskDefinition:
    return AgentTaskDefinition(
        task_id=task_id,
        name="example",
        prompt="run something",
        schedule=DelayedTaskSchedule(delay_seconds=60, repeat=False),
    )


def _cron_definition(task_id: str = "task-cron") -> AgentTaskDefinition:
    return AgentTaskDefinition(
        task_id=task_id,
        name="cron-example",
        prompt="check every hour",
        schedule=CronTaskSchedule(expression="0 * * * *"),
    )


class TestProgressLogPath:
    def test_default_path_uses_tasks_dir(self, workspace_settings):
        scheduler = AgentTaskScheduler(chat_service=object())  # type: ignore[arg-type]
        definition = _delayed_definition()
        path = scheduler._progress_log_path(definition)
        assert path == settings.TASKS_DIR / definition.task_id / "progress.jsonl"

    def test_goal_override_wins(self, workspace_settings, tmp_path: Path):
        scheduler = AgentTaskScheduler(chat_service=object())  # type: ignore[arg-type]
        custom = tmp_path / "custom" / "log.jsonl"
        definition = _delayed_definition()
        definition.goal = AgentTaskGoal(progress_log_path=str(custom))
        assert scheduler._progress_log_path(definition) == custom


class TestProgressEntryAppending:
    async def test_append_writes_jsonl_with_required_fields(self, workspace_settings):
        scheduler = AgentTaskScheduler(chat_service=object())  # type: ignore[arg-type]
        definition = _delayed_definition("task-progress")
        run_record = AgentTaskRunRecord(
            state="completed",
            action="cancel",
            started_at=datetime(2026, 5, 17, 9, 0, 0, tzinfo=UTC),
            finished_at=datetime(2026, 5, 17, 9, 0, 5, tzinfo=UTC),
            response_text="all done",
        )
        await scheduler._append_progress_entry(definition, revision=1, run_record=run_record, trigger="scheduler")

        path = settings.TASKS_DIR / "task-progress" / "progress.jsonl"
        contents = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(contents) == 1
        entry = json.loads(contents[0])
        assert entry["task_id"] == "task-progress"
        assert entry["state"] == "completed"
        assert entry["action"] == "cancel"
        assert entry["trigger"] == "scheduler"
        assert entry["schedule_kind"] == "delayed"
        assert entry["prompt_head"] == "run something"
        assert entry["response_head"] == "all done"
        assert "goal" not in entry  # definition has no goal

    async def test_append_includes_goal_when_present(self, workspace_settings):
        scheduler = AgentTaskScheduler(chat_service=object())  # type: ignore[arg-type]
        definition = _cron_definition("task-with-goal")
        definition.goal = AgentTaskGoal(
            done_condition="all checks passed",
            max_steps_per_run=2,
            forbidden_actions=("send_message",),
        )
        run_record = AgentTaskRunRecord(
            state="completed",
            action="continue",
            response_text="ok",
        )
        await scheduler._append_progress_entry(definition, revision=3, run_record=run_record, trigger="control")

        path = settings.TASKS_DIR / "task-with-goal" / "progress.jsonl"
        entry = json.loads(path.read_text(encoding="utf-8").strip())
        assert entry["revision"] == 3
        assert entry["trigger"] == "control"
        assert entry["goal"] == {
            "done_condition": "all checks passed",
            "validation_prompt": None,
            "max_steps_per_run": 2,
            "max_cost_per_run_usd": None,
            "forbidden_actions": ["send_message"],
        }


class TestForbiddenActionsGate:
    async def test_send_message_denied_when_forbidden(self, workspace_settings):
        session_binding.bind_mcp_session_to_runtime_session("mcp-task-1", "task:abc")
        task_runtime_state.set_task_forbidden_actions("task:abc", ("send_message",))

        class _Ctx:
            session_id = "mcp-task-1"

        result = await mcp_mod.send_message("cli", "hello", ctx=_Ctx())  # type: ignore[arg-type]
        assert result["status"] == "error"
        assert result["type"] == "denied"
        assert result["details"]["reason"] == "task_forbidden_action"
        assert result["details"]["forbidden_actions"] == ["send_message"]

    async def test_unrelated_tool_passes_through(self, workspace_settings):
        session_binding.bind_mcp_session_to_runtime_session("mcp-task-2", "task:xyz")
        task_runtime_state.set_task_forbidden_actions("task:xyz", ("send_message",))

        class _Ctx:
            session_id = "mcp-task-2"

        # read_file is not forbidden; the gate must not block it.
        (settings.WORKSPACE_ROOT / "demo.txt").write_text("hello\n", encoding="utf-8")
        result = await mcp_mod.read_file("demo.txt", ctx=_Ctx())  # type: ignore[arg-type]
        assert result.get("status") != "error", result
        assert result["content"] == "hello\n"

    async def test_forbidden_blocks_subaction_for_manage_agent_task(self, workspace_settings):
        session_binding.bind_mcp_session_to_runtime_session("mcp-task-3", "task:def")
        task_runtime_state.set_task_forbidden_actions("task:def", ("manage_agent_task",))

        class _Ctx:
            session_id = "mcp-task-3"

        # The gate is invoked with `manage_agent_task.create`; the prefix-aware match denies it.
        result = await mcp_mod.manage_agent_task(
            action="create",
            name="x",
            prompt="y",
            schedule_type="delayed",
            delay_seconds=60,
            ctx=_Ctx(),  # type: ignore[arg-type]
        )
        assert result["status"] == "error"
        assert result["type"] == "denied"
        assert result["details"]["reason"] == "task_forbidden_action"


class TestAgentTaskGoalSchema:
    def test_goal_fields_optional(self):
        empty = AgentTaskGoal()
        assert empty.done_condition is None
        assert empty.forbidden_actions == ()
        assert empty.max_steps_per_run is None

    def test_max_steps_must_be_positive(self):
        with pytest.raises(ValueError):
            AgentTaskGoal(max_steps_per_run=0)

    def test_max_cost_cannot_be_negative(self):
        with pytest.raises(ValueError):
            AgentTaskGoal(max_cost_per_run_usd=-0.01)

    def test_definition_round_trip_with_goal(self):
        definition = _delayed_definition("rt-1")
        definition.goal = AgentTaskGoal(
            done_condition="done",
            max_steps_per_run=2,
            forbidden_actions=("execute_command",),
        )
        payload = definition.model_dump_json()
        restored = AgentTaskDefinition.model_validate_json(payload)
        assert restored.goal is not None
        assert restored.goal.done_condition == "done"
        assert restored.goal.max_steps_per_run == 2
        assert restored.goal.forbidden_actions == ("execute_command",)
