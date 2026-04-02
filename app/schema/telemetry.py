"""
Schema definitions for runtime telemetry payloads.
"""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schema.control import RuntimeAuthConfiguration


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RuntimeMetadata(BaseModel):
    runtime_id: str = Field(min_length=1, description="Stable runtime identifier for this isolated Pillbug instance.")
    project: str = Field(min_length=1, description="The runtime project name.")
    version: str = Field(min_length=1, description="The running application version.")
    agent_name: str | None = Field(default=None, description="Optional operator-facing agent name, when configured.")
    started_at: datetime = Field(
        default_factory=_utcnow,
        description="UTC timestamp when this runtime boot record was created.",
    )
    timezone: str = Field(min_length=1, description="Configured runtime timezone.")
    workspace_root: str = Field(
        min_length=1,
        description="Absolute workspace root path enforced by the MCP file tools.",
    )
    model: str = Field(min_length=1, description="Configured AI model identifier.")
    enabled_channels: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Enabled inbound channels for this runtime instance.",
    )


class TelemetryEvent(BaseModel):
    event_id: str = Field(min_length=1, description="Opaque unique identifier for the telemetry event.")
    event_type: str = Field(
        min_length=1, description="Stable event type identifier such as session.response.completed."
    )
    source: str = Field(min_length=1, description="Runtime subsystem that emitted the event.")
    level: Literal["info", "warning", "error"] = Field(
        default="info",
        description="Severity level for the event.",
    )
    message: str = Field(min_length=1, description="Human-readable summary of the event.")
    data: dict[str, Any] = Field(default_factory=dict, description="Structured metadata for the event.")
    occurred_at: datetime = Field(default_factory=_utcnow, description="UTC timestamp when the event was emitted.")


class HealthStatus(BaseModel):
    status: Literal["ok"] = Field(default="ok", description="Simple health status for load balancers and dashboards.")
    runtime_id: str = Field(min_length=1, description="Stable runtime identifier for this isolated Pillbug instance.")
    started_at: datetime = Field(description="UTC timestamp when the runtime started.")
    uptime_seconds: float = Field(ge=0, description="Elapsed runtime uptime in seconds.")
    last_activity_at: datetime | None = Field(
        default=None, description="UTC timestamp of the most recent recorded activity."
    )
    active_session_count: int = Field(
        ge=0, description="Number of active or recently seen sessions tracked by the runtime."
    )
    scheduler_started: bool = Field(default=False, description="Whether the embedded scheduler has started.")
    enabled_channels: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Configured inbound channels for this runtime instance.",
    )


class RuntimeTelemetrySnapshot(BaseModel):
    metadata: RuntimeMetadata
    auth_configuration: RuntimeAuthConfiguration
    uptime_seconds: float = Field(ge=0, description="Elapsed runtime uptime in seconds.")
    active_session_count: int = Field(ge=0, description="Number of tracked active sessions.")
    pending_session_count: int = Field(
        ge=0, description="Number of sessions that currently have debounced work pending."
    )
    scheduler_started: bool = Field(default=False, description="Whether the embedded scheduler has started.")
    last_activity_at: datetime | None = Field(
        default=None, description="UTC timestamp of the most recent runtime activity."
    )
    last_error_at: datetime | None = Field(
        default=None, description="UTC timestamp of the most recent error-level event."
    )
    recent_event_count: int = Field(ge=0, description="Number of retained in-memory telemetry events.")
    recent_error_count: int = Field(ge=0, description="Number of retained error-level telemetry events.")
    generated_at: datetime = Field(
        default_factory=_utcnow, description="UTC timestamp when the snapshot was generated."
    )


class ChannelTelemetryEntry(BaseModel):
    name: str = Field(min_length=1, description="Channel name such as cli or telegram.")
    destination_kind: str = Field(min_length=1, description="Destination addressing mode for outbound sends.")
    enabled: bool = Field(default=True, description="Whether the channel is enabled in runtime configuration.")
    active: bool = Field(default=False, description="Whether the channel plugin has been instantiated in-process.")
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional channel-specific configuration details safe to expose through telemetry.",
    )
    known_destinations: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Known conversation destinations seen for this channel.",
    )
    known_destination_count: int = Field(ge=0, description="Number of known destinations for the channel.")


class ChannelsTelemetrySnapshot(BaseModel):
    runtime_id: str = Field(min_length=1, description="Stable runtime identifier for this isolated Pillbug instance.")
    enabled_channels: tuple[str, ...] = Field(default_factory=tuple, description="Configured enabled channel names.")
    channels: list[ChannelTelemetryEntry] = Field(default_factory=list, description="Per-channel runtime telemetry.")
    generated_at: datetime = Field(
        default_factory=_utcnow, description="UTC timestamp when the snapshot was generated."
    )


class SessionTelemetryEntry(BaseModel):
    session_key: str = Field(min_length=1, description="Composite channel session key for the conversation.")
    channel_name: str = Field(min_length=1, description="Inbound channel name for the session.")
    conversation_id: str = Field(min_length=1, description="Channel-scoped conversation identifier.")
    user_id: str | None = Field(default=None, description="Optional user identifier associated with the session.")
    message_count: int = Field(ge=0, description="Number of inbound messages observed for this session.")
    pending_message_count: int = Field(ge=0, description="Number of debounced inbound messages currently buffered.")
    blocked_message_count: int = Field(ge=0, description="Number of blocked inbound batches observed for this session.")
    error_count: int = Field(ge=0, description="Number of processing failures observed for this session.")
    created_at: datetime = Field(description="UTC timestamp when the session was first seen by the runtime.")
    last_message_at: datetime | None = Field(default=None, description="UTC timestamp of the latest inbound message.")
    last_response_at: datetime | None = Field(
        default=None, description="UTC timestamp of the latest outbound response."
    )
    last_activity_at: datetime = Field(description="UTC timestamp of the latest activity for the session.")
    last_command: str | None = Field(
        default=None, description="Most recent recognized runtime command, when applicable."
    )


class SessionsTelemetrySnapshot(BaseModel):
    runtime_id: str = Field(min_length=1, description="Stable runtime identifier for this isolated Pillbug instance.")
    active_session_count: int = Field(ge=0, description="Number of tracked active sessions.")
    pending_session_count: int = Field(ge=0, description="Number of sessions with pending debounced messages.")
    sessions: list[SessionTelemetryEntry] = Field(
        default_factory=list, description="Tracked sessions ordered by recent activity."
    )
    generated_at: datetime = Field(
        default_factory=_utcnow, description="UTC timestamp when the snapshot was generated."
    )


class TaskExecutionTelemetry(BaseModel):
    key: str = Field(min_length=1, description="Execution key for the scheduled task instance.")
    state: str = Field(min_length=1, description="Current Docket execution state.")
    when: datetime | None = Field(default=None, description="Next or scheduled execution time, when known.")
    started_at: datetime | None = Field(default=None, description="UTC timestamp when execution started, when known.")
    completed_at: datetime | None = Field(
        default=None, description="UTC timestamp when execution completed, when known."
    )
    error: str | None = Field(default=None, description="Execution error text, when present.")


class TaskRunTelemetry(BaseModel):
    task_id: str = Field(min_length=1, description="Stable task identifier.")
    task_name: str = Field(min_length=1, description="Operator-visible task name.")
    state: Literal["completed", "failed"] = Field(description="Persisted result state from the latest execution.")
    action: Literal["continue", "cancel"] = Field(description="Model-selected continuation action for the run.")
    started_at: datetime = Field(description="UTC timestamp when the task run started.")
    finished_at: datetime = Field(description="UTC timestamp when the task run finished.")
    response_text: str | None = Field(default=None, description="Persisted task response text, when present.")
    error: str | None = Field(default=None, description="Persisted task error string, when present.")


class AgentTaskTelemetryEntry(BaseModel):
    task_id: str = Field(min_length=1, description="Stable task identifier.")
    name: str = Field(min_length=1, description="Operator-visible task name.")
    schedule_kind: Literal["cron", "delayed", "perpetual"] = Field(description="Normalized schedule kind for the task.")
    schedule_detail: str = Field(
        default="",
        description="Human-readable schedule detail such as the cron expression or delay interval.",
    )
    enabled: bool = Field(default=True, description="Whether the task is currently enabled.")
    revision: int = Field(ge=1, description="Current task definition revision.")
    created_at: datetime = Field(description="UTC timestamp when the task was created.")
    updated_at: datetime = Field(description="UTC timestamp when the task definition last changed.")
    last_run: TaskRunTelemetry | None = Field(default=None, description="Latest persisted run record for the task.")
    execution: TaskExecutionTelemetry | None = Field(
        default=None, description="Current execution metadata, when scheduled or running."
    )


class SchedulerTelemetrySnapshot(BaseModel):
    started: bool = Field(default=False, description="Whether the embedded scheduler is currently running.")
    backend: str = Field(min_length=1, description="Scheduler backend kind such as memory or redis.")
    total_tasks: int = Field(ge=0, description="Total persisted task count.")
    enabled_tasks: int = Field(ge=0, description="Number of enabled tasks.")
    cron_tasks: int = Field(ge=0, description="Number of cron-scheduled tasks.")
    delayed_tasks: int = Field(ge=0, description="Number of delayed tasks.")
    running_executions: int = Field(ge=0, description="Number of currently running Docket executions.")
    scheduled_executions: int = Field(ge=0, description="Number of queued future Docket executions.")
    recent_runs: list[TaskRunTelemetry] = Field(
        default_factory=list, description="Most recent task runs across all tasks."
    )


class TasksTelemetrySnapshot(BaseModel):
    runtime_id: str = Field(min_length=1, description="Stable runtime identifier for this isolated Pillbug instance.")
    scheduler: SchedulerTelemetrySnapshot
    tasks: list[AgentTaskTelemetryEntry] = Field(
        default_factory=list, description="Persisted task definitions with execution metadata."
    )
    generated_at: datetime = Field(
        default_factory=_utcnow, description="UTC timestamp when the snapshot was generated."
    )
