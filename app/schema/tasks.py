"""
Schema definitions for scheduled background agent tasks.
"""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, Field, model_validator

from app.core.config import settings


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AgentTaskRunRecord(BaseModel):
    state: Literal["completed", "failed"]
    action: Literal["continue", "cancel"] = "continue"
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime = Field(default_factory=_utcnow)
    response_text: str | None = None
    error: str | None = None


class AgentTaskGoal(BaseModel):
    """Optional per-task goal contract (plan P2 #12).

    Every field is optional and backward-compatible: an existing scheduled task without
    a goal continues to run with the defaults defined elsewhere. The scheduler reads
    these fields to bound a single run (`max_steps_per_run`, `forbidden_actions`) and
    to feed the progress log (`done_condition`, `validation_prompt`).
    """

    done_condition: str | None = Field(
        default=None,
        description="Human-readable success criterion shown to the model and logged on each run.",
    )
    validation_prompt: str | None = Field(
        default=None,
        description="Optional follow-up prompt the scheduler should record alongside each run.",
    )
    max_steps_per_run: int | None = Field(
        default=None,
        ge=1,
        description="Cap for Gemini AFC remote tool calls in a single run; None means use the global default.",
    )
    max_cost_per_run_usd: float | None = Field(
        default=None,
        ge=0,
        description="Advisory cost ceiling in USD. Currently only recorded, not enforced (plan P2 #12 note).",
    )
    forbidden_actions: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Tool names blocked for this task's run, on top of any planning-mode gates.",
    )
    progress_log_path: str | None = Field(
        default=None,
        description="Optional override for the per-task progress log path; defaults to tasks/<task_id>/progress.jsonl.",
    )


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
    clean_session: bool = True
    goal: AgentTaskGoal | None = Field(
        default=None,
        description="Optional per-task goal contract (max steps, forbidden actions, etc.).",
    )
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
        return f"agent-task:{settings.runtime_id}:{self.task_id}"

    @property
    def function_name(self) -> str:
        return f"agent-task:{settings.runtime_id}:{self.task_id}"

    @property
    def resolved_session_id(self) -> str:
        return f"{self.session_id}"


class AgentTaskStore(BaseModel):
    tasks: list[AgentTaskDefinition] = Field(default_factory=list)
