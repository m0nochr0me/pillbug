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
    get_channel_plugin,
    load_channel_plugins,
    register_channel_conversation,
    unregister_channel_plugin,
)
from app.runtime.pipeline import InboundProcessingPipeline
from app.runtime.session_binding import bind_runtime_session_origin_metadata
from app.schema.messages import (
    A2AEnvelope,
    InboundBatch,
    InboundMessage,
    ProcessedInboundMessage,
    extract_a2a_origin_channel_metadata,
)
from app.schema.telemetry import SessionsTelemetrySnapshot, SessionTelemetryEntry

_SUMMARIZE_PROMPT_NAME = "summarize.prompt.md"
_COMPRESS_PROMPT_NAME = "compress.prompt.md"
_SESSION_COMPRESSED_MESSAGE = "Session compressed"


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
        self._listener_tasks: list[asyncio.Task[None]] = []
        self._session_state_by_key: dict[str, _SessionTelemetryState] = {}
        self._session_summarization_locks: dict[str, asyncio.Lock] = {}
        self._drain_requested = False
        self._shutdown_requested = False
        self._shutdown_event = asyncio.Event()
        runtime_telemetry.bind_application_loop(self)

    @property
    def is_draining(self) -> bool:
        return self._drain_requested

    @property
    def is_shutdown_requested(self) -> bool:
        return self._shutdown_requested

    async def wait_for_shutdown(self) -> None:
        await self._shutdown_event.wait()

    async def clear_session(self, session_id: str) -> tuple[str, int]:
        session_key = self._resolve_session_key(session_id)
        dropped_message_count = self._drop_pending_messages_for_session(session_key)
        self._sessions[session_key] = await self._chat_service.reset_session(session_key)

        state = self._session_state_by_key.get(session_key)
        now = _utcnow()
        if state is not None:
            state.last_command = "/clear"
            state.last_response_at = now
            state.last_activity_at = now
            state.pending_message_count = self._pending_message_count_for_session(session_key)

        channel_name = state.channel_name if state is not None else session_key.partition(":")[0]
        logger.info(f"Cleared session history for {session_key} via control API")
        await runtime_telemetry.record_event(
            event_type="session.control.clear",
            source="application-loop",
            message="Session cleared through control API.",
            data={
                "session_key": session_key,
                "channel": channel_name,
                "dropped_pending_messages": dropped_message_count,
            },
        )
        return session_key, dropped_message_count

    async def request_drain(self, *, reason: str = "operator") -> bool:
        if self._drain_requested:
            return False

        self._drain_requested = True
        logger.info(f"Runtime drain requested reason={reason}")
        await runtime_telemetry.record_event(
            event_type="runtime.drain.requested",
            source="application-loop",
            message="Runtime drain requested.",
            data={"reason": reason},
        )

        for listener_task in tuple(self._listener_tasks):
            listener_task.cancel()

        return True

    async def request_shutdown(self, *, reason: str = "operator") -> bool:
        if self._shutdown_requested:
            return False

        self._shutdown_requested = True
        self._shutdown_event.set()
        logger.info(f"Runtime shutdown requested reason={reason}")
        await runtime_telemetry.record_event(
            event_type="runtime.shutdown.requested",
            source="application-loop",
            message="Runtime shutdown requested.",
            data={"reason": reason},
        )
        await self.request_drain(reason=reason)
        return True

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
        self._listener_tasks = listener_tasks
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

            self._listener_tasks = []

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
        response_policy = self._channel_response_policy(channel, batch.last_message)

        if response_policy is not None and not getattr(response_policy, "should_process", True):
            reply_text = getattr(response_policy, "reply_text", None)
            if isinstance(reply_text, str) and reply_text.strip():
                response_sent = await self._send_inbound_response(
                    channel=channel,
                    inbound_message=batch.last_message,
                    response_text=reply_text,
                )
                if response_sent:
                    self._record_session_response(batch.session_key)
                else:
                    self._record_session_activity(batch.session_key)
            else:
                self._record_session_activity(batch.session_key)

            await runtime_telemetry.record_event(
                event_type="session.response.stopped",
                source="application-loop",
                message="Inbound message was stopped by channel response policy before model execution.",
                data={
                    "session_key": batch.session_key,
                    "channel": batch.channel_name,
                    "conversation_id": batch.conversation_id,
                    "reason": getattr(response_policy, "reason", None),
                },
            )
            return

        if await self._handle_command(batch, channel):
            return

        if processed_message.security.blocked:
            self._record_blocked_batch(batch)
            rejection = self._render_security_rejection(processed_message)
            await self._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=rejection,
            )
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
            response_sent = await self._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text="Session cleared. Started a new chat session.",
            )
            logger.info(f"Cleared session history for {batch.session_key}")
            if response_sent:
                self._record_command_response(batch, command)
            else:
                self._record_command_invocation(batch, command)
                self._record_session_activity(batch.session_key)
            await runtime_telemetry.record_event(
                event_type="session.command.clear",
                source="application-loop",
                message="Session cleared through runtime command.",
                data={"session_key": batch.session_key, "channel": batch.channel_name},
            )
            return True

        session = await self._get_session(batch.session_key)

        if command == "/usage":
            response_sent = await self._send_inbound_response(
                channel=channel,
                inbound_message=batch.last_message,
                response_text=session.render_usage_report(),
            )
            logger.info(f"Reported session token usage for {batch.session_key}")
            if response_sent:
                self._record_command_response(batch, command)
            else:
                self._record_command_invocation(batch, command)
                self._record_session_activity(batch.session_key)
            await runtime_telemetry.record_event(
                event_type="session.command.usage",
                source="application-loop",
                message="Usage report returned through runtime command.",
                data={"session_key": batch.session_key, "channel": batch.channel_name},
            )
            return True

        if command == "/summarize":
            try:
                summarize_prompt = self._chat_service.render_prompt_text(_SUMMARIZE_PROMPT_NAME)
            except Exception:
                logger.exception(f"Failed to load summarize prompt for {batch.session_key}")
                await self._send_inbound_response(
                    channel=channel,
                    inbound_message=batch.last_message,
                    response_text="I could not load the summarize prompt right now. Please try again.",
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
        bind_runtime_session_origin_metadata(batch.session_key, batch.last_message.metadata)
        response_channel, response_inbound_message, use_source_response_policy = self._resolve_response_target(
            channel,
            batch.last_message,
        )

        try:
            async with response_channel.response_presence(response_inbound_message):
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
            await self._send_resolved_response(
                source_channel=channel,
                source_inbound_message=batch.last_message,
                response_channel=response_channel,
                response_inbound_message=response_inbound_message,
                use_source_response_policy=use_source_response_policy,
                response_text="I could not process that message right now. Please try again.",
            )
            return

        if response.usage_metadata is not None:
            logger.info(f"Completed response for {batch.session_key}: {response.usage_metadata.model_dump_json()}")

        response_text = response.text.strip()
        if not response_text:
            response_text = "I could not produce a text response right now. Please try again."
            logger.warning(f"Model response was blank for {batch.session_key}; using runtime fallback text")

        response_sent = await self._send_resolved_response(
            source_channel=channel,
            source_inbound_message=batch.last_message,
            response_channel=response_channel,
            response_inbound_message=response_inbound_message,
            use_source_response_policy=use_source_response_policy,
            response_text=response_text,
        )
        if response_sent:
            self._record_session_response(batch.session_key)
        else:
            self._record_session_activity(batch.session_key)
        await runtime_telemetry.record_event(
            event_type="session.response.completed",
            source="application-loop",
            message="Session response completed.",
            data={
                "session_key": batch.session_key,
                "channel": batch.channel_name,
                "message_count": batch.message_count,
                "response_chars": len(response_text),
                "response_sent": response_sent,
            },
        )
        await self._maybe_auto_summarize_session(
            channel=channel,
            batch=batch,
            session=session,
        )

    async def _maybe_auto_summarize_session(
        self,
        *,
        channel: ChannelPlugin,
        batch: InboundBatch,
        session: GeminiChatSession,
    ) -> None:
        summarization_mode = settings.SESSION_SUMMARIZATION
        if summarization_mode is None:
            return

        if session.total_token_count() <= settings.SESSION_SUMMARIZATION_THRESHOLD:
            return

        summarization_lock = self._session_summarization_locks.setdefault(batch.session_key, asyncio.Lock())
        if summarization_lock.locked():
            return

        async with summarization_lock:
            total_token_count = session.total_token_count()
            if total_token_count <= settings.SESSION_SUMMARIZATION_THRESHOLD:
                return

            logger.info(
                f"Auto-summarizing session {batch.session_key} mode={summarization_mode} total_tokens={total_token_count}"
            )
            await runtime_telemetry.record_event(
                event_type="session.summarization.started",
                source="application-loop",
                message="Automatic session summarization started.",
                data={
                    "session_key": batch.session_key,
                    "channel": batch.channel_name,
                    "mode": summarization_mode,
                    "total_token_count": total_token_count,
                    "threshold": settings.SESSION_SUMMARIZATION_THRESHOLD,
                },
            )

            try:
                if summarization_mode == "memory":
                    summarize_prompt = self._chat_service.render_prompt_text(_SUMMARIZE_PROMPT_NAME)
                    await session.send_message(summarize_prompt)
                    self._sessions[batch.session_key] = await self._chat_service.reset_session(batch.session_key)
                else:
                    compress_prompt = self._chat_service.render_prompt_text(_COMPRESS_PROMPT_NAME)
                    compression_response = await session.send_message(compress_prompt)
                    compression_summary = compression_response.text.strip()
                    if not compression_summary:
                        raise RuntimeError("Compression summary response was blank")

                    await session.replace_history_with_summary(compression_summary)

                response_sent = await self._send_inbound_response(
                    channel=channel,
                    inbound_message=batch.last_message,
                    response_text=_SESSION_COMPRESSED_MESSAGE,
                )
                if response_sent:
                    self._record_session_response(batch.session_key)
                else:
                    self._record_session_activity(batch.session_key)

                await runtime_telemetry.record_event(
                    event_type="session.summarization.completed",
                    source="application-loop",
                    message="Automatic session summarization completed.",
                    data={
                        "session_key": batch.session_key,
                        "channel": batch.channel_name,
                        "mode": summarization_mode,
                        "response_sent": response_sent,
                    },
                )
            except Exception:
                logger.exception(f"Failed to auto-summarize session {batch.session_key}")
                self._record_session_error(batch.session_key)
                await runtime_telemetry.record_event(
                    event_type="session.summarization.failed",
                    source="application-loop",
                    level="error",
                    message="Automatic session summarization failed.",
                    data={
                        "session_key": batch.session_key,
                        "channel": batch.channel_name,
                        "mode": summarization_mode,
                    },
                )

    def _channel_response_policy(self, channel: ChannelPlugin, inbound_message: InboundMessage) -> object | None:
        response_policy = getattr(channel, "response_policy", None)
        if not callable(response_policy):
            return None

        try:
            return response_policy(inbound_message)
        except Exception:
            logger.exception(f"Failed to resolve channel response policy for {channel.name}")
            return None

    async def _maybe_send_channel_response(
        self,
        *,
        channel: ChannelPlugin,
        inbound_message: InboundMessage,
        response_text: str,
    ) -> bool:
        response_policy = self._channel_response_policy(channel, inbound_message)
        if response_policy is not None and not getattr(response_policy, "should_reply", True):
            reason = getattr(response_policy, "reason", None)
            logger.info(
                f"Suppressed automatic channel response for {inbound_message.session_key} channel={channel.name} reason={reason}"
            )
            await runtime_telemetry.record_event(
                event_type="session.response.suppressed",
                source="application-loop",
                message="Automatic channel response was suppressed by channel policy.",
                data={
                    "session_key": inbound_message.session_key,
                    "channel": channel.name,
                    "conversation_id": inbound_message.conversation_id,
                    "reason": reason,
                },
            )
            return False

        await channel.send_response(inbound_message, response_text)
        return True

    def _resolve_response_target(
        self,
        channel: ChannelPlugin,
        inbound_message: InboundMessage,
    ) -> tuple[ChannelPlugin, InboundMessage, bool]:
        if channel.name != "a2a":
            return channel, inbound_message, True

        try:
            envelope = A2AEnvelope.from_inbound_metadata(inbound_message.metadata)
        except ValueError:
            return channel, inbound_message, True

        origin_route = envelope.origin_route
        if origin_route is None:
            return channel, inbound_message, True

        response_channel_name, response_conversation_id = origin_route
        response_channel = self._channel_by_name.get(response_channel_name)
        if response_channel is None:
            response_channel = get_channel_plugin(response_channel_name, create=True)
            if response_channel is None:
                logger.warning(
                    "Unable to route A2A terminal reply to origin channel "
                    f"{response_channel_name} for session={inbound_message.session_key}"
                )
                return channel, inbound_message, True
            self._channel_by_name[response_channel_name] = response_channel

        response_metadata = extract_a2a_origin_channel_metadata(inbound_message.metadata) or {}
        return (
            response_channel,
            InboundMessage(
                channel_name=response_channel.name,
                conversation_id=response_conversation_id,
                text=inbound_message.text,
                user_id=inbound_message.user_id,
                metadata=response_metadata,
            ),
            False,
        )

    async def _send_inbound_response(
        self,
        *,
        channel: ChannelPlugin,
        inbound_message: InboundMessage,
        response_text: str,
    ) -> bool:
        response_channel, response_inbound_message, use_source_response_policy = self._resolve_response_target(
            channel,
            inbound_message,
        )
        return await self._send_resolved_response(
            source_channel=channel,
            source_inbound_message=inbound_message,
            response_channel=response_channel,
            response_inbound_message=response_inbound_message,
            use_source_response_policy=use_source_response_policy,
            response_text=response_text,
        )

    async def _send_resolved_response(
        self,
        *,
        source_channel: ChannelPlugin,
        source_inbound_message: InboundMessage,
        response_channel: ChannelPlugin,
        response_inbound_message: InboundMessage,
        use_source_response_policy: bool,
        response_text: str,
    ) -> bool:
        if use_source_response_policy:
            return await self._maybe_send_channel_response(
                channel=source_channel,
                inbound_message=source_inbound_message,
                response_text=response_text,
            )

        await response_channel.send_response(response_inbound_message, response_text)
        return True

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

    def _resolve_session_key(self, session_id: str) -> str:
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise ValueError("session_id must not be empty")

        if normalized_session_id in self._sessions or normalized_session_id in self._session_state_by_key:
            return normalized_session_id

        matching_session_keys = {
            session_key
            for session_key, state in self._session_state_by_key.items()
            if state.conversation_id == normalized_session_id
        }

        if len(matching_session_keys) == 1:
            return next(iter(matching_session_keys))

        if matching_session_keys:
            raise ValueError("Session identifier is ambiguous; use the full session_key value from telemetry.")

        raise ValueError(f"Session not found: {session_id}")

    def _drop_pending_messages_for_session(self, session_key: str) -> int:
        dropped_message_count = 0

        for debounce_key in tuple(self._pending_messages):
            if not debounce_key.startswith(f"{session_key}:"):
                continue

            dropped_message_count += len(self._pending_messages.pop(debounce_key, ()))
            flush_task = self._flush_tasks.pop(debounce_key, None)
            if flush_task is not None:
                flush_task.cancel()

        self._sync_pending_count(session_key)
        return dropped_message_count

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

    def _record_session_activity(self, session_key: str) -> None:
        state = self._session_state_by_key.get(session_key)
        if state is None:
            return

        state.last_activity_at = _utcnow()
        self._sync_pending_count(session_key)

    def _record_session_error(self, session_key: str) -> None:
        state = self._session_state_by_key.get(session_key)
        if state is None:
            return

        state.error_count += 1
        state.last_activity_at = _utcnow()
        self._sync_pending_count(session_key)

    def track_outbound_conversation(self, channel_name: str, conversation_id: str) -> None:
        normalized_channel_name = channel_name.strip()
        normalized_conversation_id = conversation_id.strip()
        if not normalized_channel_name or not normalized_conversation_id:
            return

        now = _utcnow()
        session_key = f"{normalized_channel_name}:{normalized_conversation_id}"
        state = self._session_state_for(
            session_key=session_key,
            channel_name=normalized_channel_name,
            conversation_id=normalized_conversation_id,
            user_id=None,
            first_seen_at=now,
        )
        state.last_activity_at = now
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
