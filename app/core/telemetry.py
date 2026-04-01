"""
Runtime telemetry broker and event stream helpers.
"""

import asyncio
from collections import deque
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import uuid4

from app import __project__, __version__
from app.core.config import settings
from app.schema.control import RuntimeAuthConfiguration
from app.schema.telemetry import (
    HealthStatus,
    RuntimeMetadata,
    RuntimeTelemetrySnapshot,
    SessionsTelemetrySnapshot,
    TasksTelemetrySnapshot,
    TelemetryEvent,
)

__all__ = ("RuntimeTelemetry", "runtime_telemetry")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _ApplicationLoopTelemetryProvider(Protocol):
    async def describe_sessions_telemetry(self) -> SessionsTelemetrySnapshot: ...


class _SchedulerTelemetryProvider(Protocol):
    async def describe_tasks_telemetry(self) -> TasksTelemetrySnapshot: ...


class RuntimeTelemetry:
    def __init__(self) -> None:
        self._metadata = RuntimeMetadata(
            runtime_id=settings.runtime_id,
            project=__project__,
            version=__version__,
            agent_name=settings.AGENT_NAME,
            timezone=settings.TIMEZONE,
            workspace_root=str(settings.WORKSPACE_ROOT),
            model=settings.GEMINI_MODEL,
            enabled_channels=settings.enabled_channels(),
        )
        self._events: deque[TelemetryEvent] = deque(maxlen=250)
        self._subscribers: set[asyncio.Queue[TelemetryEvent]] = set()
        self._lock = asyncio.Lock()
        self._last_activity_at: datetime | None = self._metadata.started_at
        self._last_error_at: datetime | None = None
        self._application_loop: _ApplicationLoopTelemetryProvider | None = None
        self._scheduler: _SchedulerTelemetryProvider | None = None

    def metadata(self) -> RuntimeMetadata:
        return self._metadata.model_copy(deep=True)

    def bind_application_loop(self, provider: _ApplicationLoopTelemetryProvider) -> None:
        self._application_loop = provider

    def bind_scheduler(self, provider: _SchedulerTelemetryProvider) -> None:
        self._scheduler = provider

    async def record_event(
        self,
        *,
        event_type: str,
        source: str,
        message: str,
        level: Literal["info", "warning", "error"] = "info",
        data: dict[str, Any] | None = None,
    ) -> TelemetryEvent:
        event = TelemetryEvent(
            event_id=uuid4().hex,
            event_type=event_type,
            source=source,
            level=level,
            message=message,
            data={key: value for key, value in (data or {}).items() if value is not None},
        )

        async with self._lock:
            self._events.append(event)
            self._last_activity_at = event.occurred_at
            if event.level == "error":
                self._last_error_at = event.occurred_at
            subscribers = tuple(self._subscribers)

        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except asyncio.QueueFull:
                continue

        return event

    async def recent_events(self, limit: int = 50) -> list[TelemetryEvent]:
        bounded_limit = max(0, min(limit, 250))
        async with self._lock:
            if bounded_limit == 0:
                return []
            return list(self._events)[-bounded_limit:]

    async def subscribe(self, replay: int = 0) -> tuple[asyncio.Queue[TelemetryEvent], list[TelemetryEvent]]:
        queue: asyncio.Queue[TelemetryEvent] = asyncio.Queue(maxsize=100)
        bounded_replay = max(0, min(replay, 250))

        async with self._lock:
            self._subscribers.add(queue)
            replay_events = list(self._events)[-bounded_replay:] if bounded_replay else []

        return queue, replay_events

    async def unsubscribe(self, queue: asyncio.Queue[TelemetryEvent]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def build_health_status(self) -> HealthStatus:
        sessions_snapshot = await self._describe_sessions()
        tasks_snapshot = await self._describe_tasks()

        async with self._lock:
            last_activity_at = self._last_activity_at

        return HealthStatus(
            runtime_id=self._metadata.runtime_id,
            started_at=self._metadata.started_at,
            uptime_seconds=max((_utcnow() - self._metadata.started_at).total_seconds(), 0.0),
            last_activity_at=last_activity_at,
            active_session_count=sessions_snapshot.active_session_count,
            scheduler_started=tasks_snapshot.scheduler.started,
            enabled_channels=self._metadata.enabled_channels,
        )

    async def build_runtime_snapshot(
        self,
        auth_configuration: RuntimeAuthConfiguration,
    ) -> RuntimeTelemetrySnapshot:
        sessions_snapshot = await self._describe_sessions()
        tasks_snapshot = await self._describe_tasks()
        recent_events = await self.recent_events(limit=250)

        async with self._lock:
            last_activity_at = self._last_activity_at
            last_error_at = self._last_error_at

        return RuntimeTelemetrySnapshot(
            metadata=self.metadata(),
            auth_configuration=auth_configuration,
            uptime_seconds=max((_utcnow() - self._metadata.started_at).total_seconds(), 0.0),
            active_session_count=sessions_snapshot.active_session_count,
            pending_session_count=sessions_snapshot.pending_session_count,
            scheduler_started=tasks_snapshot.scheduler.started,
            last_activity_at=last_activity_at,
            last_error_at=last_error_at,
            recent_event_count=len(recent_events),
            recent_error_count=sum(1 for event in recent_events if event.level == "error"),
        )

    async def build_sessions_snapshot(self) -> SessionsTelemetrySnapshot:
        return await self._describe_sessions()

    async def build_tasks_snapshot(self) -> TasksTelemetrySnapshot:
        return await self._describe_tasks()

    async def _describe_sessions(self) -> SessionsTelemetrySnapshot:
        if self._application_loop is None:
            return SessionsTelemetrySnapshot(
                runtime_id=self._metadata.runtime_id, active_session_count=0, pending_session_count=0
            )

        return await self._application_loop.describe_sessions_telemetry()

    async def _describe_tasks(self) -> TasksTelemetrySnapshot:
        if self._scheduler is None:
            return TasksTelemetrySnapshot(
                runtime_id=self._metadata.runtime_id,
                scheduler={
                    "started": False,
                    "backend": "memory",
                    "total_tasks": 0,
                    "enabled_tasks": 0,
                    "cron_tasks": 0,
                    "delayed_tasks": 0,
                    "running_executions": 0,
                    "scheduled_executions": 0,
                    "recent_runs": [],
                },  # pyright: ignore[reportArgumentType]
                tasks=[],
            )

        return await self._scheduler.describe_tasks_telemetry()


runtime_telemetry = RuntimeTelemetry()
