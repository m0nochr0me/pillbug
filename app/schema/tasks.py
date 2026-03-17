"""
Schema definitions for scheduled background agent tasks.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, Field, model_validator


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AgentTaskRunRecord(BaseModel):
    state: Literal["completed", "failed"]
    action: Literal["continue", "cancel"] = "continue"
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime = Field(default_factory=_utcnow)
    response_text: str | None = None
    error: str | None = None


class CronTaskSchedule(BaseModel):
    kind: Literal["cron"] = "cron"
    expression: str = Field(min_length=1)
    timezone: str = Field(default="UTC", min_length=1)


class DelayedTaskSchedule(BaseModel):
    kind: Literal["delayed", "perpetual"] = "delayed"
    delay_seconds: int = Field(ge=1, validation_alias=AliasChoices("delay_seconds", "every_seconds"))
    repeat: bool = False

    @model_validator(mode="after")
    def normalize_kind(self) -> Self:
        self.kind = "delayed"
        return self


TaskSchedule = Annotated[
    CronTaskSchedule | DelayedTaskSchedule,
    Field(discriminator="kind"),
]


class AgentTaskDefinition(BaseModel):
    task_id: str = Field(default_factory=lambda: uuid4().hex)
    name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    schedule: TaskSchedule
    enabled: bool = True
    revision: int = Field(default=1, ge=1)
    session_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_run: AgentTaskRunRecord | None = None

    @model_validator(mode="after")
    def populate_session_id(self) -> Self:
        if not self.session_id:
            self.session_id = f"task:{self.task_id}"

        return self

    @property
    def execution_key(self) -> str:
        return f"agent-task:{self.task_id}"

    @property
    def function_name(self) -> str:
        return f"agent-task:{self.task_id}"

    @property
    def resolved_session_id(self) -> str:
        return self.session_id


class AgentTaskStore(BaseModel):
    tasks: list[AgentTaskDefinition] = Field(default_factory=list)
