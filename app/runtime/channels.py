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

    async def send_response(self, inbound_message: InboundMessage, response_text: str) -> None:
        await asyncio.to_thread(print, f"{self._assistant_prefix}{response_text}")


ChannelFactory = Callable[[], ChannelPlugin]
"""Factory type for creating channel plugin instances."""


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


def load_channel_plugins() -> list[ChannelPlugin]:
    """
    Load channel plugins based on settings.
    """
    factories: dict[str, ChannelFactory] = {
        "cli": CliChannel,
    }

    for channel_name, import_path in settings.channel_plugin_factories().items():
        factories[channel_name] = _load_channel_factory(import_path)

    channels: list[ChannelPlugin] = []
    for channel_name in settings.enabled_channels():
        factory = factories.get(channel_name)
        if factory is None:
            raise ValueError(
                f"Enabled channel '{channel_name}' is not available. Configure PB_CHANNEL_PLUGIN_FACTORIES to register it."
            )

        channels.append(factory())

    return channels
