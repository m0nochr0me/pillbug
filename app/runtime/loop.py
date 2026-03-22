import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.ai import GeminiChatService, GeminiChatSession
from app.core.config import settings
from app.core.log import logger
from app.core.telemetry import runtime_telemetry
from app.runtime.channels import (
    ChannelPlugin,
    load_channel_plugins,
    register_channel_conversation,
    unregister_channel_plugin,
)
from app.runtime.pipeline import InboundProcessingPipeline
from app.schema.messages import InboundBatch, InboundMessage, ProcessedInboundMessage
from app.schema.telemetry import SessionsTelemetrySnapshot, SessionTelemetryEntry

_SUMMARIZE_PROMPT_NAME = "summarize.prompt.md"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class _ChannelClosed:
    channel_name: str


@dataclass(slots=True)
class _SessionTelemetryState:
    session_key: str
    channel_name: str
    conversation_id: str
    user_id: str | None
    created_at: datetime
    last_message_at: datetime | None = None
    last_response_at: datetime | None = None
    last_activity_at: datetime | None = None
    last_command: str | None = None
    message_count: int = 0
    pending_message_count: int = 0
    blocked_message_count: int = 0
    error_count: int = 0


class ApplicationLoop:
    def __init__(
        self,
        chat_service: GeminiChatService,
        channels: list[ChannelPlugin] | None = None,
        pipeline: InboundProcessingPipeline | None = None,
    ) -> None:
        self._chat_service = chat_service
        self._channels = channels or load_channel_plugins()
        self._pipeline = pipeline or InboundProcessingPipeline()
        self._debounce_window = settings.INBOUND_DEBOUNCE_SECONDS
        self._channel_by_name = {channel.name: channel for channel in self._channels}
        self._sessions: dict[str, GeminiChatSession] = {}
        self._pending_messages: dict[str, list[InboundMessage]] = {}
        self._flush_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_state_by_key: dict[str, _SessionTelemetryState] = {}
        runtime_telemetry.bind_application_loop(self)

    async def run(self) -> None:
        if not self._channels:
            raise RuntimeError("No inbound channels are configured")

        logger.info(
            f"Starting application loop with channels={list(self._channel_by_name)} debounce={self._debounce_window}s"
        )
        await runtime_telemetry.record_event(
            event_type="runtime.loop.started",
            source="application-loop",
            message="Application loop started.",
            data={"channels": list(self._channel_by_name), "debounce_seconds": self._debounce_window},
        )

        queue: asyncio.Queue[InboundMessage | _ChannelClosed] = asyncio.Queue()
        listener_tasks = [
            asyncio.create_task(self._consume_channel(channel, queue), name=f"listen:{channel.name}")
            for channel in self._channels
        ]
        open_channels = len(listener_tasks)

        try:
            while open_channels > 0 or self._flush_tasks:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.1)
                except TimeoutError:
                    continue

                if isinstance(event, _ChannelClosed):
                    open_channels -= 1
                    logger.info(f"Channel closed: {event.channel_name}")
                    await runtime_telemetry.record_event(
                        event_type="channel.closed",
                        source="application-loop",
                        message="Inbound channel closed.",
                        data={"channel": event.channel_name, "open_channels": open_channels},
                    )
                    continue

                self._schedule_flush(event)
        finally:
            for listener_task in listener_tasks:
                listener_task.cancel()

            with suppress(asyncio.CancelledError):
                await asyncio.gather(*listener_tasks, return_exceptions=True)

            for flush_task in self._flush_tasks.values():
                flush_task.cancel()

            with suppress(asyncio.CancelledError):
                await asyncio.gather(*self._flush_tasks.values(), return_exceptions=True)

            await self._close_channels()
            await runtime_telemetry.record_event(
                event_type="runtime.loop.stopped",
                source="application-loop",
                message="Application loop stopped.",
                data={"channels": list(self._channel_by_name)},
            )

    async def _consume_channel(
        self,
        channel: ChannelPlugin,
        queue: asyncio.Queue[InboundMessage | _ChannelClosed],
    ) -> None:
        try:
            async for inbound_message in channel.listen():
                await queue.put(inbound_message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"Channel listener failed: {channel.name}")
            await runtime_telemetry.record_event(
                event_type="channel.listener.failed",
                source="application-loop",
                level="error",
                message="Inbound channel listener failed.",
                data={"channel": channel.name},
            )
        finally:
            await queue.put(_ChannelClosed(channel.name))

    def _schedule_flush(
        self,
        inbound_message: InboundMessage,
    ) -> None:
        register_channel_conversation(inbound_message.channel_name, inbound_message.conversation_id)
        debounce_key = inbound_message.debounce_key
        self._pending_messages.setdefault(debounce_key, []).append(inbound_message)
        self._record_inbound_message(inbound_message)

        existing_task = self._flush_tasks.get(debounce_key)
        if existing_task is not None:
            existing_task.cancel()

        self._flush_tasks[debounce_key] = asyncio.create_task(
            self._flush_after_debounce(debounce_key),
            name=f"debounce:{debounce_key}",
        )

    async def _flush_after_debounce(
        self,
        debounce_key: str,
    ) -> None:
        try:
            await asyncio.sleep(self._debounce_window)
            await self._flush_messages(debounce_key)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"Failed to flush debounced messages for key={debounce_key}")

    async def _flush_messages(
        self,
        debounce_key: str,
    ) -> None:
        messages = tuple(self._pending_messages.pop(debounce_key, ()))
        self._flush_tasks.pop(debounce_key, None)
        if not messages:
            return

        self._sync_pending_count(messages[0].session_key)

        batch = InboundBatch(messages=messages)
        processed_message = await self._pipeline.process(batch)
        await self._respond(processed_message)

    async def _respond(
        self,
        processed_message: ProcessedInboundMessage,
    ) -> None:
        batch = processed_message.batch
        channel = self._channel_by_name[batch.channel_name]

        if await self._handle_command(batch, channel):
            return

        if processed_message.security.blocked:
            self._record_blocked_batch(batch)
            rejection = self._render_security_rejection(processed_message)
            await channel.send_response(batch.last_message, rejection)
            logger.warning(f"Blocked inbound message from {batch.session_key}: {processed_message.security.reasons}")
            await runtime_telemetry.record_event(
                event_type="session.message.blocked",
                source="application-loop",
                level="warning",
                message="Inbound message batch was blocked by security policy.",
                data={
                    "session_key": batch.session_key,
                    "channel": batch.channel_name,
                    "conversation_id": batch.conversation_id,
                    "message_count": batch.message_count,
                    "reasons": list(processed_message.security.reasons),
                },
            )
            return

        session = await self._get_session(batch.session_key)

        await self._send_session_response(
            channel=channel,
            batch=batch,
            session=session,
            model_input=processed_message.model_input,
            message_metadata=[message.metadata for message in batch.messages],
        )

    async def _handle_command(self, batch: InboundBatch, channel: ChannelPlugin) -> bool:
        command = self._recognized_command(batch.raw_text)
        if command is None:
            return False

        if command == "/clear":
            self._sessions[batch.session_key] = await self._chat_service.reset_session(batch.session_key)
            await channel.send_response(batch.last_message, "Session cleared. Started a new chat session.")
            logger.info(f"Cleared session history for {batch.session_key}")
            self._record_command_response(batch, command)
            await runtime_telemetry.record_event(
                event_type="session.command.clear",
                source="application-loop",
                message="Session cleared through runtime command.",
                data={"session_key": batch.session_key, "channel": batch.channel_name},
            )
            return True

        session = await self._get_session(batch.session_key)

        if command == "/usage":
            await channel.send_response(batch.last_message, session.render_usage_report())
            logger.info(f"Reported session token usage for {batch.session_key}")
            self._record_command_response(batch, command)
            await runtime_telemetry.record_event(
                event_type="session.command.usage",
                source="application-loop",
                message="Usage report returned through runtime command.",
                data={"session_key": batch.session_key, "channel": batch.channel_name},
            )
            return True

        if command == "/summarize":
            try:
                summarize_prompt = await self._chat_service.read_prompt_text(_SUMMARIZE_PROMPT_NAME)
            except Exception:
                logger.exception(f"Failed to load summarize prompt for {batch.session_key}")
                await channel.send_response(
                    batch.last_message,
                    "I could not load the summarize prompt right now. Please try again.",
                )
                return True

            logger.info(f"Running summarize prompt for {batch.session_key}")
            self._record_command_invocation(batch, command)
            await self._send_session_response(
                channel=channel,
                batch=batch,
                session=session,
                model_input=summarize_prompt,
            )
            return True

        return False

    async def _send_session_response(
        self,
        *,
        channel: ChannelPlugin,
        batch: InboundBatch,
        session: GeminiChatSession,
        model_input: str,
        message_metadata: list[dict[str, object]] | None = None,
    ) -> None:
        try:
            async with channel.response_presence(batch.last_message):
                response = await session.send_message(
                    model_input,
                    message_metadata=message_metadata,
                )
        except Exception:
            logger.exception(f"Failed to process inbound message for session={batch.session_key}")
            self._record_session_error(batch.session_key)
            await runtime_telemetry.record_event(
                event_type="session.response.failed",
                source="application-loop",
                level="error",
                message="Model response generation failed.",
                data={"session_key": batch.session_key, "channel": batch.channel_name},
            )
            await channel.send_response(
                batch.last_message,
                "I could not process that message right now. Please try again.",
            )
            return

        if response.usage_metadata is not None:
            logger.info(f"Completed response for {batch.session_key}: {response.usage_metadata.model_dump_json()}")

        response_text = response.text.strip()
        if not response_text:
            response_text = "I could not produce a text response right now. Please try again."
            logger.warning(f"Model response was blank for {batch.session_key}; using runtime fallback text")

        await channel.send_response(batch.last_message, response_text)
        self._record_session_response(batch.session_key)
        await runtime_telemetry.record_event(
            event_type="session.response.completed",
            source="application-loop",
            message="Session response completed.",
            data={
                "session_key": batch.session_key,
                "channel": batch.channel_name,
                "message_count": batch.message_count,
                "response_chars": len(response_text),
            },
        )

    async def _get_session(self, session_key: str) -> GeminiChatSession:
        session = self._sessions.get(session_key)
        if session is not None:
            return session

        session = await self._chat_service.restore_session(session_key)
        self._sessions[session_key] = session
        await runtime_telemetry.record_event(
            event_type="session.restored",
            source="application-loop",
            message="Session state restored for conversation.",
            data={"session_key": session_key},
        )
        return session

    def _recognized_command(self, raw_text: str) -> str | None:
        normalized_text = raw_text.strip().lower()
        if normalized_text in {"/clear", "/summarize", "/usage"}:
            return normalized_text

        return None

    def _render_security_rejection(self, processed_message: ProcessedInboundMessage) -> str:
        reasons = "; ".join(processed_message.security.reasons)
        return f"I could not accept that message: {reasons}."

    def _session_state_for(
        self,
        *,
        session_key: str,
        channel_name: str,
        conversation_id: str,
        user_id: str | None,
        first_seen_at: datetime,
    ) -> _SessionTelemetryState:
        state = self._session_state_by_key.get(session_key)
        if state is not None:
            if state.user_id is None and user_id is not None:
                state.user_id = user_id
            return state

        state = _SessionTelemetryState(
            session_key=session_key,
            channel_name=channel_name,
            conversation_id=conversation_id,
            user_id=user_id,
            created_at=first_seen_at,
            last_activity_at=first_seen_at,
        )
        self._session_state_by_key[session_key] = state
        return state

    def _pending_message_count_for_session(self, session_key: str) -> int:
        return sum(
            len(messages)
            for debounce_key, messages in self._pending_messages.items()
            if debounce_key.startswith(f"{session_key}:")
        )

    def _sync_pending_count(self, session_key: str) -> None:
        state = self._session_state_by_key.get(session_key)
        if state is None:
            return

        state.pending_message_count = self._pending_message_count_for_session(session_key)

    def _record_inbound_message(self, inbound_message: InboundMessage) -> None:
        state = self._session_state_for(
            session_key=inbound_message.session_key,
            channel_name=inbound_message.channel_name,
            conversation_id=inbound_message.conversation_id,
            user_id=inbound_message.user_id,
            first_seen_at=inbound_message.received_at,
        )
        state.message_count += 1
        state.last_message_at = inbound_message.received_at
        state.last_activity_at = inbound_message.received_at
        state.pending_message_count = self._pending_message_count_for_session(inbound_message.session_key)

    def _record_blocked_batch(self, batch: InboundBatch) -> None:
        state = self._session_state_for(
            session_key=batch.session_key,
            channel_name=batch.channel_name,
            conversation_id=batch.conversation_id,
            user_id=batch.user_id,
            first_seen_at=batch.received_at,
        )
        state.blocked_message_count += batch.message_count
        state.last_activity_at = _utcnow()
        self._sync_pending_count(batch.session_key)

    def _record_command_invocation(self, batch: InboundBatch, command: str) -> None:
        state = self._session_state_for(
            session_key=batch.session_key,
            channel_name=batch.channel_name,
            conversation_id=batch.conversation_id,
            user_id=batch.user_id,
            first_seen_at=batch.received_at,
        )
        state.last_command = command
        state.last_activity_at = _utcnow()

    def _record_command_response(self, batch: InboundBatch, command: str) -> None:
        state = self._session_state_for(
            session_key=batch.session_key,
            channel_name=batch.channel_name,
            conversation_id=batch.conversation_id,
            user_id=batch.user_id,
            first_seen_at=batch.received_at,
        )
        now = _utcnow()
        state.last_command = command
        state.last_response_at = now
        state.last_activity_at = now
        self._sync_pending_count(batch.session_key)

    def _record_session_response(self, session_key: str) -> None:
        state = self._session_state_by_key.get(session_key)
        if state is None:
            return

        now = _utcnow()
        state.last_response_at = now
        state.last_activity_at = now
        self._sync_pending_count(session_key)

    def _record_session_error(self, session_key: str) -> None:
        state = self._session_state_by_key.get(session_key)
        if state is None:
            return

        state.error_count += 1
        state.last_activity_at = _utcnow()
        self._sync_pending_count(session_key)

    async def describe_sessions_telemetry(self) -> SessionsTelemetrySnapshot:
        entries: list[SessionTelemetryEntry] = []

        for session_key, state in self._session_state_by_key.items():
            pending_message_count = self._pending_message_count_for_session(session_key)
            entries.append(
                SessionTelemetryEntry(
                    session_key=session_key,
                    channel_name=state.channel_name,
                    conversation_id=state.conversation_id,
                    user_id=state.user_id,
                    message_count=state.message_count,
                    pending_message_count=pending_message_count,
                    blocked_message_count=state.blocked_message_count,
                    error_count=state.error_count,
                    created_at=state.created_at,
                    last_message_at=state.last_message_at,
                    last_response_at=state.last_response_at,
                    last_activity_at=state.last_activity_at or state.created_at,
                    last_command=state.last_command,
                )
            )

        entries.sort(key=lambda entry: entry.last_activity_at, reverse=True)
        pending_session_count = sum(1 for entry in entries if entry.pending_message_count > 0)

        return SessionsTelemetrySnapshot(
            runtime_id=settings.runtime_id,
            active_session_count=len(entries),
            pending_session_count=pending_session_count,
            sessions=entries,
        )

    async def _close_channels(self) -> None:
        for channel in self._channels:
            try:
                await channel.close()
            except Exception:
                logger.exception(f"Failed to close channel: {channel.name}")
            finally:
                unregister_channel_plugin(channel.name)
