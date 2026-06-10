"""
Persistence for scheduled agent task definitions.

`TaskStore` owns the in-memory task map, its lock, and the JSON store file at
`{RUNTIME_BASE_DIR}/tasks/agent_tasks.json` (shape: `app.schema.tasks.AgentTaskStore`).
The scheduler holds one instance; multi-step mutations take `store.lock` and call
`persist_locked()` before releasing it.
"""

import asyncio
from pathlib import Path

from app.schema.tasks import AgentTaskDefinition, AgentTaskRunRecord, AgentTaskStore
from app.util.clock import utcnow
from app.util.workspace import async_read_text_file, async_write_text_file


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.tasks: dict[str, AgentTaskDefinition] = {}
        self.lock = asyncio.Lock()

    async def ensure_parent_dir(self) -> None:
        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)

    async def load(self) -> None:
        if not await asyncio.to_thread(self.path.is_file):
            async with self.lock:
                self.tasks = {}
            return

        raw_store = await async_read_text_file(self.path)
        store = AgentTaskStore.model_validate_json(raw_store)
        async with self.lock:
            self.tasks = {task.task_id: task for task in store.tasks}

    async def persist_locked(self) -> None:
        store = AgentTaskStore(tasks=sorted(self.tasks.values(), key=lambda task: task.created_at))
        payload = store.model_dump_json(indent=2)
        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)
        await async_write_text_file(self.path, payload, mode="w")

    async def snapshot(self, task_id: str) -> AgentTaskDefinition | None:
        async with self.lock:
            definition = self.tasks.get(task_id)
            return definition.model_copy(deep=True) if definition is not None else None

    async def snapshots(self) -> list[AgentTaskDefinition]:
        async with self.lock:
            return [task.model_copy(deep=True) for task in self.tasks.values()]

    async def record_run(
        self,
        *,
        task_id: str,
        revision: int,
        run_record: AgentTaskRunRecord,
    ) -> None:
        async with self.lock:
            definition = self.tasks.get(task_id)
            if definition is None or definition.revision != revision:
                return

            definition.last_run = run_record
            await self.persist_locked()

    async def disable(self, task_id: str, revision: int) -> None:
        async with self.lock:
            definition = self.tasks.get(task_id)
            if definition is None or definition.revision != revision or not definition.enabled:
                return

            definition.enabled = False
            definition.updated_at = utcnow()
            await self.persist_locked()
