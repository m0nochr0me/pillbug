"""Scheduled task contract: definition fields, schedule discriminator, defaults."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schema.tasks import (
    AgentTaskDefinition,
    AgentTaskStore,
    CronTaskSchedule,
    DelayedTaskSchedule,
)


class TestDelayedTaskSchedule:
    def test_delay_seconds_must_be_positive(self):
        with pytest.raises(ValidationError):
            DelayedTaskSchedule(delay_seconds=0)

    def test_accepts_every_seconds_alias(self):
        schedule = DelayedTaskSchedule.model_validate({"every_seconds": 30})
        assert schedule.delay_seconds == 30
        assert schedule.kind == "delayed"

    def test_kind_is_normalized_even_when_perpetual_is_provided(self):
        schedule = DelayedTaskSchedule.model_validate({"kind": "perpetual", "delay_seconds": 5, "repeat": True})
        assert schedule.kind == "delayed"
        assert schedule.repeat is True


class TestCronTaskSchedule:
    def test_expression_is_required(self):
        with pytest.raises(ValidationError):
            CronTaskSchedule(expression="")


class TestAgentTaskDefinition:
    def test_session_id_defaults_to_task_prefixed_value(self):
        definition = AgentTaskDefinition(
            name="check",
            prompt="run me",
            schedule=DelayedTaskSchedule(delay_seconds=30),
        )
        assert definition.session_id == f"task:{definition.task_id}"
        assert definition.resolved_session_id == definition.session_id

    def test_custom_session_id_is_preserved(self):
        definition = AgentTaskDefinition(
            name="check",
            prompt="run me",
            schedule=DelayedTaskSchedule(delay_seconds=30),
            session_id="custom-session",
        )
        assert definition.session_id == "custom-session"

    def test_execution_key_includes_runtime_id(self):
        from app.core.config import settings

        definition = AgentTaskDefinition(
            name="check",
            prompt="run me",
            schedule=CronTaskSchedule(expression="*/5 * * * *"),
        )
        assert definition.execution_key == f"agent-task:{settings.runtime_id}:{definition.task_id}"


class TestAgentTaskStore:
    def test_empty_store_round_trip(self):
        store = AgentTaskStore()
        round_tripped = AgentTaskStore.model_validate_json(store.model_dump_json())
        assert round_tripped.tasks == []
