"""Telegram channel plugin for Pillbug using Shingram."""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from shingram.client import AsyncClient
from shingram.events import Event, normalize

from app.core.log import logger
from app.runtime.channels import BaseChannel
from app.schema.messages import InboundMessage

_DEFAULT_ALLOWED_UPDATES = ("message", "edited_message")
_TEXT_EVENT_TYPES = frozenset({"message", "edited_message", "command"})
_MAX_TELEGRAM_MESSAGE_CHARS = 4000


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _chunk_message(text: str, *, max_chars: int = _MAX_TELEGRAM_MESSAGE_CHARS) -> tuple[str, ...]:
    remaining = text.strip() or " "
    chunks: list[str] = []

    while len(remaining) > max_chars:
        split_at = remaining.rfind("\n\n", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = remaining.rfind("\n", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = remaining.rfind(" ", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = max_chars

        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)

        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)

    return tuple(chunks)


class TelegramChannelSettings(BaseSettings):
    bot_token: str
    poll_timeout_seconds: int = 30
    poll_limit: int = 100
    allowed_updates: Annotated[tuple[str, ...], NoDecode] = _DEFAULT_ALLOWED_UPDATES
    reply_to_message: bool = True
    delete_webhook_on_start: bool = False
    drop_pending_updates: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PB_TELEGRAM_",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("bot_token")
    @classmethod
    def _validate_bot_token(cls, value: str) -> str:
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("PB_TELEGRAM_BOT_TOKEN is required when the Telegram channel is enabled")
        return stripped_value

    @field_validator("allowed_updates", mode="before")
    @classmethod
    def _parse_allowed_updates(cls, value: object) -> object:
        if value is None:
            return _DEFAULT_ALLOWED_UPDATES
        if isinstance(value, str):
            return _split_csv(value) or _DEFAULT_ALLOWED_UPDATES
        return value

    @field_validator("poll_timeout_seconds")
    @classmethod
    def _validate_poll_timeout_seconds(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("PB_TELEGRAM_POLL_TIMEOUT_SECONDS must be greater than zero")
        return value

    @field_validator("poll_limit")
    @classmethod
    def _validate_poll_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("PB_TELEGRAM_POLL_LIMIT must be greater than zero")
        return value

    @classmethod
    def from_env(cls) -> TelegramChannelSettings:
        return cls()  # type: ignore[call-arg]


class TelegramChannel(BaseChannel):
    name = "telegram"

    def __init__(self, settings: TelegramChannelSettings | None = None) -> None:
        self._settings = settings or TelegramChannelSettings.from_env()
        self._client = AsyncClient(self._settings.bot_token)
        self._offset = 0

    async def listen(self) -> AsyncIterator[InboundMessage]:
        if self._settings.delete_webhook_on_start:
            await self._delete_webhook()

        logger.info(
            "Starting Telegram channel polling "
            f"allowed_updates={list(self._settings.allowed_updates)} timeout={self._settings.poll_timeout_seconds}s"
        )

        try:
            while True:
                try:
                    updates = await self._client.call_async(
                        "getUpdates",
                        offset=self._offset,
                        timeout=self._settings.poll_timeout_seconds,
                        limit=self._settings.poll_limit,
                        allowed_updates=list(self._settings.allowed_updates),
                    )
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    continue
                except Exception:
                    logger.exception("Telegram polling failed")
                    await asyncio.sleep(1)
                    continue

                if not isinstance(updates, list):
                    continue

                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = update_id + 1

                    event = normalize(update)
                    inbound_message = self._build_inbound_message(event)
                    if inbound_message is not None:
                        yield inbound_message
        finally:
            await self.close()

    async def send_response(self, inbound_message: InboundMessage, response_text: str) -> None:
        chat_id = inbound_message.metadata.get("telegram_chat_id")
        if not isinstance(chat_id, int):
            raise ValueError("Telegram inbound message metadata is missing telegram_chat_id")

        reply_to_message_id = inbound_message.metadata.get("telegram_message_id")
        for index, chunk in enumerate(_chunk_message(response_text)):
            params = {
                "chat_id": chat_id,
                "text": chunk,
            }
            if index == 0 and self._settings.reply_to_message and isinstance(reply_to_message_id, int):
                params["reply_to_message_id"] = reply_to_message_id
                params["allow_sending_without_reply"] = True

            await self._client.call_async("sendMessage", **params)

    async def close(self) -> None:
        await self._client.close()

    async def _delete_webhook(self) -> None:
        await self._client.call_async(
            "deleteWebhook",
            drop_pending_updates=self._settings.drop_pending_updates,
        )
        logger.info(
            "Deleted Telegram webhook before starting long polling "
            f"drop_pending_updates={self._settings.drop_pending_updates}"
        )

    def _build_inbound_message(self, event: Event | None) -> InboundMessage | None:
        if event is None or event.type not in _TEXT_EVENT_TYPES:
            return None
        if not event.text or event.chat_id == 0:
            return None

        return InboundMessage(
            channel_name=self.name,
            conversation_id=str(event.chat_id),
            user_id=str(event.user_id) if event.user_id else None,
            text=event.text,
            metadata={
                "source": "telegram",
                "telegram_chat_id": event.chat_id,
                "telegram_message_id": event.message_id,
                "telegram_update_id": event.raw.get("update_id"),
                "telegram_event_type": event.type,
                "telegram_chat_type": event.chat_type,
                "telegram_content_type": event.content_type,
                "telegram_username": event.username,
                "telegram_first_name": event.first_name,
            },
        )


def create_channel() -> TelegramChannel:
    return TelegramChannel()
