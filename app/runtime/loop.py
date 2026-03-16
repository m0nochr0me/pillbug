import asyncio
from contextlib import suppress
from dataclasses import dataclass

from app.core.ai import GeminiChatService, GeminiChatSession
from app.core.config import settings
from app.core.log import logger
from app.runtime.channels import ChannelPlugin, load_channel_plugins
from app.runtime.pipeline import InboundProcessingPipeline
from app.schema.messages import InboundBatch, InboundMessage, ProcessedInboundMessage


@dataclass(slots=True)
class _ChannelClosed:
    channel_name: str


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

    async def run(self) -> None:
        if not self._channels:
            raise RuntimeError("No inbound channels are configured")

        logger.info(
            f"Starting application loop with channels={list(self._channel_by_name)} debounce={self._debounce_window}s"
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
        finally:
            await queue.put(_ChannelClosed(channel.name))

    def _schedule_flush(
        self,
        inbound_message: InboundMessage,
    ) -> None:
        debounce_key = inbound_message.debounce_key
        self._pending_messages.setdefault(debounce_key, []).append(inbound_message)

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

        batch = InboundBatch(messages=messages)
        processed_message = await self._pipeline.process(batch)
        await self._respond(processed_message)

    async def _respond(
        self,
        processed_message: ProcessedInboundMessage,
    ) -> None:
        batch = processed_message.batch
        channel = self._channel_by_name[batch.channel_name]

        if self._is_clear_command(batch.raw_text):
            self._sessions[batch.session_key] = await self._chat_service.reset_session(batch.session_key)
            await channel.send_response(batch.last_message, "Session cleared. Started a new chat session.")
            logger.info(f"Cleared session history for {batch.session_key}")
            return

        if processed_message.security.blocked:
            rejection = self._render_security_rejection(processed_message)
            await channel.send_response(batch.last_message, rejection)
            logger.warning(f"Blocked inbound message from {batch.session_key}: {processed_message.security.reasons}")
            return

        session = await self._get_session(batch.session_key)

        try:
            response = await session.send_message(processed_message.model_input)
        except Exception:
            logger.exception(f"Failed to process inbound message for session={batch.session_key}")
            await channel.send_response(
                batch.last_message,
                "I could not process that message right now. Please try again.",
            )
            return

        if response.usage_metadata is not None:
            logger.info(f"Completed response for {batch.session_key}: {response.usage_metadata.model_dump_json()}")

        await channel.send_response(batch.last_message, response.text)

    async def _get_session(self, session_key: str) -> GeminiChatSession:
        session = self._sessions.get(session_key)
        if session is not None:
            return session

        session = await self._chat_service.restore_session(session_key)
        self._sessions[session_key] = session
        return session

    def _is_clear_command(self, raw_text: str) -> bool:
        return raw_text.strip().lower() == "/clear"

    def _render_security_rejection(self, processed_message: ProcessedInboundMessage) -> str:
        reasons = "; ".join(processed_message.security.reasons)
        return f"I could not accept that message: {reasons}."

    async def _close_channels(self) -> None:
        for channel in self._channels:
            try:
                await channel.close()
            except Exception:
                logger.exception(f"Failed to close channel: {channel.name}")
