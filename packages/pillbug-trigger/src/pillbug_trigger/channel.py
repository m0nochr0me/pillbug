"""Trigger channel plugin — receives external events via HTTP and yields them as InboundMessages."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.core.log import logger
from app.runtime.channels import BaseChannel
from app.schema.messages import InboundMessage
from pillbug_trigger.config import settings
from pillbug_trigger.schema import TriggerEvent, TriggerSourceConfig
from pillbug_trigger.server import create_trigger_app, run_server


class TriggerChannel(BaseChannel):
    """Channel that accepts external trigger events over HTTP with urgency-based debounce."""

    name = "trigger"
    destination_kind = "source"

    def __init__(self) -> None:
        self._queue: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self._pending: dict[str, list[tuple[TriggerEvent, TriggerSourceConfig | None]]] = {}
        self._flush_tasks: dict[str, asyncio.Task[None]] = {}
        self._server_task: asyncio.Task[None] | None = None
        self._app = create_trigger_app(self._on_event)

    async def _on_event(
        self,
        event: TriggerEvent,
        source_cfg: TriggerSourceConfig | None,
        debounce_secs: float,
    ) -> None:
        """Called by the HTTP server for each accepted event. Buffers and debounces."""
        key = self._debounce_key(event, source_cfg)

        self._pending.setdefault(key, []).append((event, source_cfg))

        if key in self._flush_tasks:
            self._flush_tasks[key].cancel()

        self._flush_tasks[key] = asyncio.create_task(self._flush_after_debounce(key, debounce_secs))

    async def _flush_after_debounce(self, key: str, delay: float) -> None:
        """Wait for the debounce window then flush buffered events as a single InboundMessage."""
        await asyncio.sleep(delay)

        pending_events = self._pending.pop(key, [])
        self._flush_tasks.pop(key, None)
        self._app.state.pending_counts.pop(key, None)

        if not pending_events:
            return

        first, source_cfg = pending_events[0]
        events = [event for event, _ in pending_events]
        effective_urgency = source_cfg.urgency_override if source_cfg and source_cfg.urgency_override else first.urgency

        text = self._build_message_text(events, source_cfg)
        conversation_id = first.conversation_id or first.source

        metadata: dict[str, Any] = {
            "trigger_source": first.source,
            "trigger_urgency": effective_urgency.value,
            "trigger_event_count": len(events),
            "trigger_events": [e.model_dump(mode="json") for e in events],
        }

        message = InboundMessage(
            channel_name=self.name,
            conversation_id=conversation_id,
            text=text,
            user_id=f"trigger:{first.source}",
            metadata=metadata,
        )

        await self._queue.put(message)
        logger.info(
            "Trigger batch flushed",
            source=first.source,
            urgency=effective_urgency.value,
            event_count=len(events),
            conversation_id=conversation_id,
        )

    def _build_message_text(
        self,
        events: list[TriggerEvent],
        source_cfg: TriggerSourceConfig | None,
    ) -> str:
        """Build the text payload sent to the agent from batched events."""
        if len(events) == 1:
            event = events[0]
            if source_cfg:
                return source_cfg.prompt.format(title=event.title, body=event.body)
            return f"[Trigger: {event.source}] {event.title}\n{event.body}".strip()

        parts: list[str] = []
        header_source = events[0].source
        if source_cfg:
            parts.append(f"Batch of {len(events)} trigger events from '{header_source}':\n")
        else:
            parts.append(f"[Trigger: {header_source}] Batch of {len(events)} events:\n")

        for i, event in enumerate(events, 1):
            if source_cfg:
                parts.append(f"--- Event {i} ---")
                parts.append(source_cfg.prompt.format(title=event.title, body=event.body))
            else:
                parts.append(f"--- Event {i}: {event.title} ---")
                if event.body:
                    parts.append(event.body)
            parts.append("")

        return "\n".join(parts).strip()

    def _debounce_key(
        self,
        event: TriggerEvent,
        source_cfg: TriggerSourceConfig | None,
    ) -> str:
        urgency = event.urgency
        if source_cfg and source_cfg.urgency_override is not None:
            urgency = source_cfg.urgency_override
        conversation = event.conversation_id or event.source
        return f"{event.source}:{conversation}:{urgency}"

    async def listen(self) -> AsyncIterator[InboundMessage]:
        ready = asyncio.Event()
        self._server_task = asyncio.create_task(run_server(self._app, ready=ready))
        await ready.wait()
        logger.info("Trigger channel listening", host=settings.HOST, port=settings.PORT)

        try:
            while True:
                message = await self._queue.get()
                yield message
        except asyncio.CancelledError:
            return

    async def send_message(
        self,
        conversation_id: str,
        message_text: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        logger.info("Trigger channel outbound message", conversation_id=conversation_id, length=len(message_text))

    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
    ) -> None:
        logger.info(
            "Trigger channel agent response",
            conversation_id=inbound_message.conversation_id,
            source=inbound_message.metadata.get("trigger_source", "unknown"),
            response_length=len(response_text),
        )

    async def close(self) -> None:
        for task in self._flush_tasks.values():
            task.cancel()
        self._flush_tasks.clear()

        if self._server_task is not None:
            self._server_task.cancel()
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass

        logger.info("Trigger channel closed")


def create_channel() -> TriggerChannel:
    return TriggerChannel()
