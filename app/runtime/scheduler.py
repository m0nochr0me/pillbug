"""
Background agent task scheduling built on Docket.
"""

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from docket import Cron, Docket, Perpetual, Worker

from app.core.ai import GeminiChatService, chat_service
from app.core.config import settings
from app.core.log import logger
from app.core.telemetry import runtime_telemetry
from app.schema.tasks import (
    AgentTaskDefinition,
    AgentTaskRunRecord,
    AgentTaskStore,
    CronTaskSchedule,
    DelayedTaskSchedule,
    TaskSchedule,
)
from app.schema.telemetry import (
    AgentTaskTelemetryEntry,
    SchedulerTelemetrySnapshot,
    TaskExecutionTelemetry,
    TaskRunTelemetry,
    TasksTelemetrySnapshot,
)
from app.util.workspace import async_read_text_file, async_write_text_file

__all__ = ("AgentTaskScheduler", "task_scheduler")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AgentTaskScheduler:
    def __init__(
        self,
        chat_service: GeminiChatService,
        store_path: Path | None = None,
    ) -> None:
        self._chat_service = chat_service
        self._store_path = store_path or settings.TASKS_STORE_PATH
        self._tasks: dict[str, AgentTaskDefinition] = {}
        self._lock = asyncio.Lock()
        self._startup_lock = asyncio.Lock()
        self._docket: Docket | None = None
        self._worker: Worker | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._started = False
        runtime_telemetry.bind_scheduler(self)

    async def ensure_started(self) -> None:
        if self._started:
            return

        async with self._startup_lock:
            if self._started:
                return

            await self._load_store()
            await asyncio.to_thread(self._store_path.parent.mkdir, parents=True, exist_ok=True)

            docket = Docket(
                name=settings.DOCKET_NAME,
                url=settings.docket_url(),
                execution_ttl=timedelta(seconds=settings.DOCKET_EXECUTION_TTL_SECONDS),
            )
            await docket.__aenter__()

            worker = Worker(
                docket=docket,
                concurrency=settings.DOCKET_WORKER_CONCURRENCY,
                redelivery_timeout=timedelta(seconds=settings.DOCKET_REDELIVERY_TIMEOUT_SECONDS),
                schedule_automatic_tasks=False,
            )
            await worker.__aenter__()

            self._docket = docket
            self._worker = worker
            self._worker_task = asyncio.create_task(worker.run_forever(), name="pillbug:docket-worker")
            self._started = True
            await runtime_telemetry.record_event(
                event_type="scheduler.started",
                source="scheduler",
                message="Embedded task scheduler started.",
                data={"backend": self._scheduler_backend(), "task_count": len(self._tasks)},
            )

            try:
                for definition in self._tasks.values():
                    self._register_task(definition)

                await self._schedule_missing_tasks()
            except Exception:
                self._started = False
                await self.aclose()
                raise

    async def aclose(self) -> None:
        worker = self._worker
        docket = self._docket
        worker_task = self._worker_task

        self._worker = None
        self._docket = None
        self._worker_task = None
        self._started = False

        if worker is not None:
            await worker.__aexit__(None, None, None)

        if worker_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task

        if docket is not None:
            await docket.__aexit__(None, None, None)

        await runtime_telemetry.record_event(
            event_type="scheduler.stopped",
            source="scheduler",
            message="Embedded task scheduler stopped.",
            data={"backend": self._scheduler_backend()},
        )

    async def list_tasks(self) -> dict[str, Any]:
        await self.ensure_started()
        definitions = await self._task_snapshots()
        tasks = [await self._serialize_task(definition) for definition in definitions]
        return {
            "tasks": tasks,
            "count": len(tasks),
        }

    async def get_task(self, task_id: str) -> dict[str, Any]:
        await self.ensure_started()
        definition = await self._task_snapshot(task_id)
        if definition is None:
            raise ValueError(f"Task not found: {task_id}")

        return {
            "task": await self._serialize_task(definition),
        }

    async def create_task(
        self,
        *,
        name: str,
        prompt: str,
        schedule_type: str,
        cron_expression: str | None = None,
        delay_seconds: int | None = None,
        timezone_name: str | None = None,
        enabled: bool = True,
        repeat: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_started()

        definition = AgentTaskDefinition(
            name=name.strip(),
            prompt=prompt.strip(),
            schedule=self._build_schedule(
                schedule_type=schedule_type,
                cron_expression=cron_expression,
                delay_seconds=delay_seconds,
                timezone_name=timezone_name,
                repeat=repeat,
            ),
            enabled=enabled,
        )

        async with self._lock:
            self._tasks[definition.task_id] = definition
            await self._persist_locked()

        self._register_task(definition)
        if definition.enabled:
            await self._schedule_task(definition, replace=False)

        logger.info(f"Created scheduled agent task {definition.task_id}")
        await runtime_telemetry.record_event(
            event_type="scheduler.task.created",
            source="scheduler",
            message="Scheduled task created.",
            data={"task_id": definition.task_id, "name": definition.name, "schedule_kind": definition.schedule.kind},
        )
        return {
            "task": await self._serialize_task(definition),
        }

    async def update_task(
        self,
        task_id: str,
        *,
        name: str | None = None,
        prompt: str | None = None,
        schedule_type: str | None = None,
        cron_expression: str | None = None,
        delay_seconds: int | None = None,
        timezone_name: str | None = None,
        enabled: bool | None = None,
        repeat: bool | None = None,
    ) -> dict[str, Any]:
        await self.ensure_started()

        async with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                raise ValueError(f"Task not found: {task_id}")

            updated = current.model_copy(deep=True)
            changed = False

            if name is not None and name.strip() != updated.name:
                updated.name = name.strip()
                changed = True

            if prompt is not None and prompt.strip() != updated.prompt:
                updated.prompt = prompt.strip()
                changed = True

            if (
                schedule_type is not None
                or cron_expression is not None
                or delay_seconds is not None
                or timezone_name is not None
                or repeat is not None
            ):
                target_schedule_type = schedule_type or updated.schedule.kind
                updated.schedule = self._build_schedule(
                    schedule_type=target_schedule_type,
                    cron_expression=cron_expression or self._cron_expression(updated.schedule),
                    delay_seconds=delay_seconds if delay_seconds is not None else self._delay_seconds(updated.schedule),
                    timezone_name=timezone_name or self._timezone_name(updated.schedule),
                    repeat=repeat if repeat is not None else self._repeat_enabled(updated.schedule),
                )
                changed = True

            if enabled is not None and enabled != updated.enabled:
                updated.enabled = enabled
                changed = True

            if not changed:
                return {
                    "task": await self._serialize_task(updated),
                }

            updated.revision += 1
            updated.updated_at = _utcnow()
            self._tasks[task_id] = updated
            await self._persist_locked()

        self._register_task(updated)
        if updated.enabled:
            await self._schedule_task(updated, replace=True)
        else:
            await self._cancel_task(updated.execution_key)

        logger.info(f"Updated scheduled agent task {task_id} to revision {updated.revision}")
        await runtime_telemetry.record_event(
            event_type="scheduler.task.updated",
            source="scheduler",
            message="Scheduled task updated.",
            data={
                "task_id": updated.task_id,
                "name": updated.name,
                "revision": updated.revision,
                "enabled": updated.enabled,
                "schedule_kind": updated.schedule.kind,
            },
        )
        return {
            "task": await self._serialize_task(updated),
        }

    async def delete_task(self, task_id: str) -> dict[str, Any]:
        await self.ensure_started()

        async with self._lock:
            definition = self._tasks.pop(task_id, None)
            if definition is None:
                raise ValueError(f"Task not found: {task_id}")

            await self._persist_locked()

        await self._cancel_task(definition.execution_key)
        logger.info(f"Deleted scheduled agent task {task_id}")
        await runtime_telemetry.record_event(
            event_type="scheduler.task.deleted",
            source="scheduler",
            message="Scheduled task deleted.",
            data={"task_id": task_id, "name": definition.name},
        )
        return {
            "task_id": task_id,
            "deleted": True,
        }

    async def execute_registered_task(
        self,
        task_id: str,
        revision: int,
        behavior: Perpetual,
    ) -> dict[str, str]:
        definition = await self._task_snapshot(task_id)
        if definition is None or not definition.enabled or definition.revision != revision:
            behavior.cancel()
            return {"action": "cancel", "message": "Task definition is stale or disabled."}

        started_at = _utcnow()
        raw_response = ""
        parsed_message = ""
        action = self._default_task_action(definition.schedule.kind)
        error: str | None = None

        await runtime_telemetry.record_event(
            event_type="scheduler.task.run.started",
            source="scheduler",
            message="Scheduled task execution started.",
            data={"task_id": task_id, "revision": revision, "schedule_kind": definition.schedule.kind},
        )

        try:
            session = await self._chat_service.restore_session(definition.resolved_session_id)
            response = await session.send_message(self._build_model_input(definition))
            raw_response = response.text or ""
            action, parsed_message = self._parse_task_response(raw_response, definition.schedule.kind)
            if isinstance(definition.schedule, DelayedTaskSchedule) and not self._repeat_enabled(definition.schedule):
                action = "cancel"
            return {
                "action": action,
                "message": parsed_message,
            }
        except Exception as exc:
            error = str(exc)
            await runtime_telemetry.record_event(
                event_type="scheduler.task.run.failed",
                source="scheduler",
                level="error",
                message="Scheduled task execution failed.",
                data={"task_id": task_id, "revision": revision, "error": error},
            )
            raise
        finally:
            latest = await self._task_snapshot(task_id)
            if latest is None or not latest.enabled or latest.revision != revision:
                behavior.cancel()

            if isinstance(definition.schedule, DelayedTaskSchedule) and action == "cancel":
                behavior.cancel()
                await self._disable_task(task_id, revision)

            await self._record_run(
                task_id=task_id,
                revision=revision,
                run_record=AgentTaskRunRecord(
                    state="failed" if error else "completed",
                    action=action,
                    started_at=started_at,
                    finished_at=_utcnow(),
                    response_text=parsed_message or raw_response or None,
                    error=error,
                ),
            )

            if error is None:
                await runtime_telemetry.record_event(
                    event_type="scheduler.task.run.completed",
                    source="scheduler",
                    message="Scheduled task execution completed.",
                    data={"task_id": task_id, "revision": revision, "action": action},
                )

    async def _load_store(self) -> None:
        if not await asyncio.to_thread(self._store_path.is_file):
            async with self._lock:
                self._tasks = {}
            return

        raw_store = await async_read_text_file(self._store_path)
        store = AgentTaskStore.model_validate_json(raw_store)
        async with self._lock:
            self._tasks = {task.task_id: task for task in store.tasks}

    async def _persist_locked(self) -> None:
        store = AgentTaskStore(tasks=sorted(self._tasks.values(), key=lambda task: task.created_at))
        payload = store.model_dump_json(indent=2)
        await asyncio.to_thread(self._store_path.parent.mkdir, parents=True, exist_ok=True)
        await async_write_text_file(self._store_path, payload, mode="w")

    async def _task_snapshot(self, task_id: str) -> AgentTaskDefinition | None:
        async with self._lock:
            definition = self._tasks.get(task_id)
            return definition.model_copy(deep=True) if definition is not None else None

    async def _task_snapshots(self) -> list[AgentTaskDefinition]:
        async with self._lock:
            return [task.model_copy(deep=True) for task in self._tasks.values()]

    async def _record_run(
        self,
        *,
        task_id: str,
        revision: int,
        run_record: AgentTaskRunRecord,
    ) -> None:
        async with self._lock:
            definition = self._tasks.get(task_id)
            if definition is None or definition.revision != revision:
                return

            definition.last_run = run_record
            await self._persist_locked()

    async def _disable_task(self, task_id: str, revision: int) -> None:
        async with self._lock:
            definition = self._tasks.get(task_id)
            if definition is None or definition.revision != revision or not definition.enabled:
                return

            definition.enabled = False
            definition.updated_at = _utcnow()
            await self._persist_locked()

    async def _schedule_missing_tasks(self) -> None:
        if self._docket is None:
            return

        snapshot = await self._docket.snapshot()
        active_keys = {execution.key for execution in snapshot.future} | {
            execution.key for execution in snapshot.running
        }

        for definition in await self._task_snapshots():
            if not definition.enabled or definition.execution_key in active_keys:
                continue

            await self._schedule_task(definition, replace=False)

    async def _schedule_task(self, definition: AgentTaskDefinition, *, replace: bool) -> None:
        if self._docket is None:
            raise RuntimeError("Task scheduler is not started")

        function = self._register_task(definition)
        initial_when = self._initial_when(definition)

        if replace:
            when = initial_when or _utcnow()
            await self._docket.replace(function, when, definition.execution_key)()
            return

        await self._docket.add(function, when=initial_when, key=definition.execution_key)()

    async def _cancel_task(self, execution_key: str) -> None:
        if self._docket is None:
            return

        existing = await self._docket.get_execution(execution_key)
        if existing is None:
            return

        await self._docket.cancel(execution_key)

    def _register_task(self, definition: AgentTaskDefinition):
        if self._docket is None:
            raise RuntimeError("Task scheduler is not started")

        task_id = definition.task_id
        revision = definition.revision

        if isinstance(definition.schedule, CronTaskSchedule):
            behavior = self._cron_behavior(definition.schedule)

            async def run_cron_task(schedule: Cron = behavior) -> dict[str, str]:
                return await self.execute_registered_task(task_id, revision, schedule)

            task_function = run_cron_task

        else:
            behavior = Perpetual(every=timedelta(seconds=definition.schedule.delay_seconds), automatic=False)

            async def run_perpetual_task(schedule: Perpetual = behavior) -> dict[str, str]:
                return await self.execute_registered_task(task_id, revision, schedule)

            task_function = run_perpetual_task

        task_function.__name__ = f"agent_task_{task_id}"
        task_function.__qualname__ = task_function.__name__
        task_function.__doc__ = f"Pillbug scheduled task {task_id}"

        self._docket.register(task_function, names=[definition.function_name])
        return task_function

    def _initial_when(self, definition: AgentTaskDefinition) -> datetime | None:
        if isinstance(definition.schedule, CronTaskSchedule):
            return self._cron_behavior(definition.schedule).initial_when

        if isinstance(definition.schedule, DelayedTaskSchedule):
            return _utcnow() + timedelta(seconds=definition.schedule.delay_seconds)

        return None

    def _cron_behavior(self, schedule: CronTaskSchedule) -> Cron:
        try:
            timezone = ZoneInfo(schedule.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {schedule.timezone}") from exc

        return Cron(schedule.expression, automatic=False, tz=timezone)

    def _build_schedule(
        self,
        *,
        schedule_type: str,
        cron_expression: str | None,
        delay_seconds: int | None,
        timezone_name: str | None,
        repeat: bool,
    ) -> TaskSchedule:
        normalized_type = schedule_type.strip().lower()
        if normalized_type == "cron":
            if not cron_expression or not cron_expression.strip():
                raise ValueError("cron_expression is required for cron tasks")

            resolved_timezone = (timezone_name or settings.TIMEZONE).strip()
            try:
                ZoneInfo(resolved_timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"Unknown timezone: {resolved_timezone}") from exc

            return CronTaskSchedule(
                expression=cron_expression.strip(),
                timezone=resolved_timezone,
            )

        if normalized_type in {"delayed", "perpetual"}:
            if delay_seconds is None:
                raise ValueError("delay_seconds is required for delayed tasks")

            return DelayedTaskSchedule(delay_seconds=delay_seconds, repeat=repeat)

        raise ValueError(f"Unsupported schedule_type: {schedule_type}")

    def _cron_expression(self, schedule: TaskSchedule) -> str | None:
        if isinstance(schedule, CronTaskSchedule):
            return schedule.expression
        return None

    def _delay_seconds(self, schedule: TaskSchedule) -> int | None:
        if isinstance(schedule, DelayedTaskSchedule):
            return schedule.delay_seconds
        return None

    def _repeat_enabled(self, schedule: TaskSchedule) -> bool:
        if isinstance(schedule, DelayedTaskSchedule):
            return schedule.repeat
        return False

    def _default_task_action(self, schedule_kind: str) -> Literal["continue", "cancel"]:
        if schedule_kind == "cron":
            return "continue"

        return "cancel"

    def _timezone_name(self, schedule: TaskSchedule) -> str | None:
        if isinstance(schedule, CronTaskSchedule):
            return schedule.timezone
        return None

    def _build_model_input(self, definition: AgentTaskDefinition) -> str:
        schedule_description = self._schedule_description(definition.schedule)
        if definition.schedule.kind == "cron":
            response_contract = (
                "Return a JSON object with keys action and message. The action must be continue for cron tasks."
            )
        elif self._repeat_enabled(definition.schedule):
            response_contract = (
                "Return a JSON object with keys action and message. "
                "This is a repeat-enabled delayed task. Use action=continue only when it should schedule itself again after the same delay; otherwise use action=cancel."
            )
        else:
            response_contract = (
                "Return a JSON object with keys action and message. "
                "This is a one-shot delayed task. It will be cancelled after this execution, so action should be cancel."
            )

        return "\n".join(
            (
                "Scheduled background task execution.",
                f"task_id: {definition.task_id}",
                f"task_name: {definition.name}",
                f"task_type: {self._model_task_type(definition.schedule)}",
                f"schedule: {schedule_description}",
                f"session_id: {definition.resolved_session_id}",
                "",
                "Use MCP tools as needed to complete the task.",
                response_contract,
                "",
                "Task prompt:",
                definition.prompt,
            )
        )

    def _schedule_description(self, schedule: TaskSchedule) -> str:
        if isinstance(schedule, CronTaskSchedule):
            return f"cron={schedule.expression} timezone={schedule.timezone}"

        return f"delay={schedule.delay_seconds}s repeat={str(schedule.repeat).lower()}"

    def _model_task_type(self, schedule: TaskSchedule) -> str:
        if isinstance(schedule, CronTaskSchedule):
            return "cron_task"

        if schedule.repeat:
            return "repeat_enabled_delayed_task"

        return "one_shot_delayed_task"

    def _parse_task_response(
        self,
        response_text: str,
        schedule_kind: str,
    ) -> tuple[Literal["continue", "cancel"], str]:
        stripped = response_text.strip()
        default_action = self._default_task_action(schedule_kind)
        if not stripped:
            return default_action, ""

        payload: Any = None
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start >= 0 and end > start:
                try:
                    payload = json.loads(stripped[start : end + 1])
                except json.JSONDecodeError:
                    payload = None

        if isinstance(payload, dict):
            action = str(payload.get("action", default_action)).strip().lower()
            if action not in {"continue", "cancel"}:
                action = default_action

            if schedule_kind == "cron":
                action = "continue"

            normalized_action: Literal["continue", "cancel"] = "cancel" if action == "cancel" else "continue"

            message = payload.get("message")
            if isinstance(message, str):
                return normalized_action, message.strip()

            return normalized_action, stripped

        return default_action, stripped

    async def _serialize_task(self, definition: AgentTaskDefinition) -> dict[str, Any]:
        return {
            **definition.model_dump(mode="json", exclude={"session_id"}),
            "execution_key": definition.execution_key,
            "function_name": definition.function_name,
            "execution": await self._describe_execution(definition.execution_key),
        }

    async def _describe_execution(self, execution_key: str) -> dict[str, Any] | None:
        if self._docket is None:
            return None

        execution = await self._docket.get_execution(execution_key)
        if execution is None:
            return None

        await execution.sync()
        state = getattr(execution.state, "value", execution.state)
        return {
            "key": execution.key,
            "state": state,
            "when": execution.when.isoformat() if execution.when else None,
            "started_at": execution.started_at.isoformat() if execution.started_at else None,
            "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
            "error": execution.error,
        }

    def _scheduler_backend(self) -> str:
        return settings.docket_url().partition("://")[0] or "memory"

    async def _execution_telemetry_by_key(self) -> dict[str, TaskExecutionTelemetry]:
        if self._docket is None:
            return {}

        snapshot = await self._docket.snapshot()
        telemetry_by_key: dict[str, TaskExecutionTelemetry] = {}

        for execution in [*snapshot.future, *snapshot.running]:
            state = getattr(execution.state, "value", execution.state)
            telemetry_by_key[execution.key] = TaskExecutionTelemetry(
                key=execution.key,
                state=str(state),
                when=execution.when,
                started_at=execution.started_at,
                completed_at=execution.completed_at,
                error=execution.error,
            )

        return telemetry_by_key

    def _last_run_telemetry(self, definition: AgentTaskDefinition) -> TaskRunTelemetry | None:
        if definition.last_run is None:
            return None

        return TaskRunTelemetry(
            task_id=definition.task_id,
            task_name=definition.name,
            state=definition.last_run.state,
            action=definition.last_run.action,
            started_at=definition.last_run.started_at,
            finished_at=definition.last_run.finished_at,
            response_text=definition.last_run.response_text,
            error=definition.last_run.error,
        )

    async def describe_tasks_telemetry(self) -> TasksTelemetrySnapshot:
        definitions = await self._task_snapshots() if self._started or self._tasks else []
        execution_by_key = await self._execution_telemetry_by_key() if self._started else {}

        task_entries: list[AgentTaskTelemetryEntry] = []
        recent_runs: list[TaskRunTelemetry] = []

        for definition in definitions:
            last_run = self._last_run_telemetry(definition)
            if last_run is not None:
                recent_runs.append(last_run)

            task_entries.append(
                AgentTaskTelemetryEntry(
                    task_id=definition.task_id,
                    name=definition.name,
                    schedule_kind=definition.schedule.kind,
                    enabled=definition.enabled,
                    revision=definition.revision,
                    created_at=definition.created_at,
                    updated_at=definition.updated_at,
                    last_run=last_run,
                    execution=execution_by_key.get(definition.execution_key),
                )
            )

        task_entries.sort(key=lambda entry: entry.updated_at, reverse=True)
        recent_runs.sort(key=lambda run: run.finished_at, reverse=True)

        scheduler_snapshot = SchedulerTelemetrySnapshot(
            started=self._started,
            backend=self._scheduler_backend(),
            total_tasks=len(definitions),
            enabled_tasks=sum(1 for definition in definitions if definition.enabled),
            cron_tasks=sum(1 for definition in definitions if definition.schedule.kind == "cron"),
            delayed_tasks=sum(1 for definition in definitions if definition.schedule.kind == "delayed"),
            running_executions=sum(1 for execution in execution_by_key.values() if execution.state == "running"),
            scheduled_executions=sum(1 for execution in execution_by_key.values() if execution.state != "running"),
            recent_runs=recent_runs[:20],
        )

        return TasksTelemetrySnapshot(
            runtime_id=settings.runtime_id,
            scheduler=scheduler_snapshot,
            tasks=task_entries,
        )


task_scheduler = AgentTaskScheduler(chat_service=chat_service)
