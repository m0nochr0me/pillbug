"""
Channel management for Pillbug.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib import import_module
from typing import Protocol, cast

from app.core.cache import cache
from app.core.config import settings
from app.core.log import logger
from app.schema.messages import InboundMessage


class ChannelPlugin(Protocol):
    name: str
    destination_kind: str

    def listen(self) -> AsyncIterator[InboundMessage]: ...

    async def send_message(
        self,
        conversation_id: str,
        message_text: str,
    ) -> None: ...

    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
    ) -> None: ...

    def response_presence(
        self,
        inbound_message: InboundMessage,
    ) -> AbstractAsyncContextManager[None]: ...

    async def close(self) -> None: ...


class BaseChannel(ABC):
    name: str
    destination_kind: str

    @abstractmethod
    def listen(self) -> AsyncIterator[InboundMessage]:
        raise NotImplementedError

    @abstractmethod
    async def send_message(
        self,
        conversation_id: str,
        message_text: str,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
    ) -> None:
        raise NotImplementedError

    @asynccontextmanager
    async def response_presence(
        self,
        inbound_message: InboundMessage,
    ) -> AsyncIterator[None]:
        del inbound_message
        yield

    async def close(self) -> None:
        return None


class CliChannel(BaseChannel):
    name = "cli"
    destination_kind = "implicit"

    def __init__(
        self,
        prompt: str = "you> ",
        assistant_prefix: str = "pillbug> ",
    ) -> None:
        self._prompt = prompt
        self._assistant_prefix = assistant_prefix

    async def listen(self) -> AsyncIterator[InboundMessage]:
        while True:
            try:
                text = await asyncio.to_thread(input, self._prompt)
            except EOFError:
                return

            stripped_text = text.strip()
            if not stripped_text:
                continue

            if stripped_text.lower() in {"/exit", "/quit"}:
                return

            yield InboundMessage(
                channel_name=self.name,
                conversation_id="default",
                user_id="local-user",
                text=text,
                metadata={"source": "stdin"},
            )

    async def send_message(
        self,
        conversation_id: str,
        message_text: str,
    ) -> None:
        del conversation_id
        await asyncio.to_thread(print, f"{self._assistant_prefix}{message_text}")

    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
    ) -> None:
        await self.send_message(inbound_message.conversation_id, response_text)


ChannelFactory = Callable[[], ChannelPlugin]
"""Factory type for creating channel plugin instances."""

_active_channels: dict[str, ChannelPlugin] = {}
_known_channel_conversations: dict[str, set[str]] = {}
_channel_conversation_sync_tasks: dict[str, asyncio.Task[None]] = {}
_KNOWN_CHANNEL_CONVERSATIONS_CACHE_KEY_PREFIX = "runtime:channel-conversations"


def _known_channel_conversations_cache_key(channel_name: str) -> str:
    return f"{_KNOWN_CHANNEL_CONVERSATIONS_CACHE_KEY_PREFIX}:{channel_name}"


async def _get_cached_channel_conversations(channel_name: str) -> set[str]:
    cached_conversations = await cache.get(_known_channel_conversations_cache_key(channel_name))
    if not isinstance(cached_conversations, list | tuple | set):
        return set()

    return {
        conversation_id.strip()
        for conversation_id in cached_conversations
        if isinstance(conversation_id, str) and conversation_id.strip()
    }


async def _sync_known_channel_conversations(channel_name: str) -> None:
    known_conversations = sorted(_known_channel_conversations.get(channel_name, ()))
    await cache.set(
        _known_channel_conversations_cache_key(channel_name),
        known_conversations,
        ttl=settings.CACHE_TTL,
    )


def _track_channel_conversation_sync_task(
    channel_name: str,
    task: asyncio.Task[None],
) -> None:
    _channel_conversation_sync_tasks[channel_name] = task

    def cleanup(done_task: asyncio.Task[None]) -> None:
        current_task = _channel_conversation_sync_tasks.get(channel_name)
        if current_task is done_task:
            _channel_conversation_sync_tasks.pop(channel_name, None)

        try:
            done_task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(f"Failed to sync known channel conversations for {channel_name}")

    task.add_done_callback(cleanup)


def _channel_send_target(channel: ChannelPlugin) -> str:
    if channel.destination_kind == "implicit":
        return channel.name

    return f"{channel.name}:<{channel.destination_kind}>"


def _load_channel_factory(import_path: str) -> ChannelFactory:
    """
    Dynamically load a channel factory from an import path.
    """
    module_path, separator, attribute_name = import_path.partition(":")
    if not separator or not module_path or not attribute_name:
        raise ValueError(f"Invalid channel plugin factory path: {import_path}")

    module = import_module(module_path)
    factory = getattr(module, attribute_name, None)
    if not callable(factory):
        raise TypeError(f"Channel plugin factory is not callable: {import_path}")

    return cast("ChannelFactory", factory)


def _get_channel_factories() -> dict[str, ChannelFactory]:
    factories: dict[str, ChannelFactory] = {
        "cli": CliChannel,
    }

    for channel_name, import_path in settings.channel_plugin_factories().items():
        factories[channel_name] = _load_channel_factory(import_path)

    return factories


def get_channel_plugin(
    channel_name: str,
    *,
    create: bool = False,
) -> ChannelPlugin | None:
    channel = _active_channels.get(channel_name)
    if channel is not None or not create:
        return channel

    if channel_name not in settings.enabled_channels():
        return None

    factory = _get_channel_factories().get(channel_name)
    if factory is None:
        raise ValueError(
            f"Enabled channel '{channel_name}' is not available. Configure PB_CHANNEL_PLUGIN_FACTORIES to register it."
        )

    channel = factory()
    _active_channels[channel_name] = channel
    return channel


def unregister_channel_plugin(channel_name: str) -> None:
    _active_channels.pop(channel_name, None)


def register_channel_conversation(
    channel_name: str,
    conversation_id: str,
) -> None:
    normalized_conversation_id = conversation_id.strip()
    if not normalized_conversation_id:
        return

    _known_channel_conversations.setdefault(channel_name, set()).add(normalized_conversation_id)
    existing_task = _channel_conversation_sync_tasks.get(channel_name)
    if existing_task is not None:
        existing_task.cancel()

    try:
        sync_task = asyncio.create_task(
            _sync_known_channel_conversations(channel_name),
            name=f"channel-conversations:{channel_name}",
        )
    except RuntimeError:
        return

    _track_channel_conversation_sync_task(channel_name, sync_task)


async def get_available_channels_context() -> list[str]:
    channels: list[str] = []

    for channel_name in settings.enabled_channels():
        channel = get_channel_plugin(channel_name, create=True)
        if channel is None:
            continue

        if channel.destination_kind == "implicit":
            channels.append(channel.name)
        else:
            known_destinations = sorted(
                _known_channel_conversations.get(channel_name, set())
                | await _get_cached_channel_conversations(channel_name)
            )
            if known_destinations:
                channels.extend(f"{channel.name}:{destination}" for destination in known_destinations)
            else:
                channels.append(_channel_send_target(channel))

    return channels


def load_channel_plugins() -> list[ChannelPlugin]:
    """
    Load channel plugins based on settings.
    """
    channels: list[ChannelPlugin] = []
    for channel_name in settings.enabled_channels():
        channel = get_channel_plugin(channel_name, create=True)
        if channel is None:
            raise ValueError(
                f"Enabled channel '{channel_name}' is not available. Configure PB_CHANNEL_PLUGIN_FACTORIES to register it."
            )

        channels.append(channel)

    return channels
