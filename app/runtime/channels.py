"""
Channel management for Pillbug.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from importlib import import_module
from typing import Protocol, cast

from app.core.config import settings
from app.schema.messages import InboundMessage


class ChannelPlugin(Protocol):
    name: str

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

    async def close(self) -> None: ...


class BaseChannel(ABC):
    name: str

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

    async def close(self) -> None:
        return None


class CliChannel(BaseChannel):
    name = "cli"

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

    async def send_message(self, conversation_id: str, message_text: str) -> None:
        del conversation_id
        await asyncio.to_thread(print, f"{self._assistant_prefix}{message_text}")

    async def send_response(self, inbound_message: InboundMessage, response_text: str) -> None:
        await self.send_message(inbound_message.conversation_id, response_text)


ChannelFactory = Callable[[], ChannelPlugin]
"""Factory type for creating channel plugin instances."""

_active_channels: dict[str, ChannelPlugin] = {}
_known_channel_conversations: dict[str, set[str]] = {"cli": {"default"}}


def _channel_destination_kind(channel_name: str) -> str:
    if channel_name == "cli":
        return "implicit"
    if channel_name == "telegram":
        return "chat_id"

    return "conversation_id"


def _channel_send_target(channel_name: str) -> str:
    if channel_name == "cli":
        return "cli"

    return f"{channel_name}:<{_channel_destination_kind(channel_name)}>"


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


def get_channel_plugin(channel_name: str, *, create: bool = False) -> ChannelPlugin | None:
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


def register_channel_conversation(channel_name: str, conversation_id: str) -> None:
    normalized_conversation_id = conversation_id.strip()
    if not normalized_conversation_id:
        return

    _known_channel_conversations.setdefault(channel_name, set()).add(normalized_conversation_id)


def get_available_channels_context() -> list[str]:

    channels = []

    for channel_name in settings.enabled_channels():
        destination_kind = _channel_destination_kind(channel_name)
        if destination_kind == "implicit":
            channels = [*channels, channel_name]
        else:
            known_destinations = sorted(_known_channel_conversations.get(channel_name, ()))
            channels = [*channels, *[f"{channel_name}:{destination}" for destination in known_destinations]]

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
