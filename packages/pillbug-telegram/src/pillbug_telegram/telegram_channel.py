"""Telegram channel plugin for Pillbug using Shingram."""

import asyncio
import mimetypes
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from shingram.client import AsyncClient
from shingram.events import Event, normalize
from shingram.exceptions import TelegramAPIError

from app.core.config import settings
from app.core.log import ThrottledExceptionLogger, logger
from app.runtime.channels import BaseChannel, register_channel_conversation
from app.schema.messages import InboundMessage, OutboundAttachment
from app.util.workspace import async_write_bytes_file, display_path

_DEFAULT_ALLOWED_UPDATES = ("message", "edited_message")
_TEXT_EVENT_TYPES = frozenset({"message", "edited_message", "command"})
_DOWNLOADABLE_EVENT_TYPES = ("photo", "video", "document", "audio", "voice")
_MAX_TELEGRAM_MESSAGE_CHARS = 4000
_TELEGRAM_DOWNLOADS_DIR = settings.WORKSPACE_ROOT / "inbox" / "telegram"
_FILENAME_SANITIZER = re.compile(r"[^A-Za-z0-9._-]+")
_TRANSIENT_TELEGRAM_LOG_COOLDOWN_SECONDS = 60.0
_TYPING_INTERVAL_SECONDS = 5.0
_VOICE_MIME_TYPES = frozenset({"audio/ogg", "audio/opus"})
_PHOTO_MIME_PREFIXES = ("image/jpeg", "image/png", "image/gif", "image/webp")
_TELEGRAM_FILE_UPLOAD_TIMEOUT_SECONDS = 120.0
_MAX_TYPING_ACTIONS = 10
_BOT_COMMANDS = (
    {"command": "start", "description": "Check that the bot is ready"},
    {"command": "summarize", "description": "Summarize the conversation history"},
    {"command": "usage", "description": "Show usage statistics for the current conversation"},
    {"command": "clear", "description": "Clear the current session"},
)
_ATTACHMENT_LABELS = {
    "audio": "audio file",
    "document": "file attachment",
    "photo": "photo",
    "video": "video",
    "voice": "voice message",
}
_MIME_EXTENSION_OVERRIDES = {
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
}


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _parse_chat_ids(value: str) -> list[int]:
    chat_ids: list[int] = []
    for item in value.split(","):
        stripped_item = item.strip()
        if not stripped_item:
            continue

        try:
            chat_ids.append(int(stripped_item))
        except ValueError as exc:
            raise ValueError("PB_TELEGRAM_ALLOWED_CHAT_IDS must be a comma-separated list of integer chat IDs") from exc

    return chat_ids


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


def _message_payload_from_event(event: Event) -> dict | None:
    for key in ("message", "edited_message", "channel_post"):
        payload = event.raw.get(key)
        if isinstance(payload, dict):
            return payload

    return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped_value = value.strip()
        if not stripped_value:
            return None
        try:
            return int(stripped_value)
        except ValueError:
            return None

    return None


def _extract_attachment_payload(message_payload: dict) -> tuple[str, dict] | None:
    for content_type in _DOWNLOADABLE_EVENT_TYPES:
        payload = message_payload.get(content_type)
        if content_type == "photo" and isinstance(payload, list):
            for photo_size in reversed(payload):
                if isinstance(photo_size, dict):
                    return content_type, photo_size
        if isinstance(payload, dict):
            return content_type, payload

    return None


def _sanitize_filename(value: str) -> str:
    sanitized = _FILENAME_SANITIZER.sub("_", value).strip("._")
    return sanitized or "attachment"


def _pick_file_extension(
    *,
    content_type: str,
    original_file_name: str | None,
    telegram_file_path: str,
    mime_type: str | None,
) -> str:
    for candidate in (original_file_name, telegram_file_path):
        if candidate:
            suffix = Path(candidate).suffix
            if suffix:
                return suffix

    if mime_type:
        override = _MIME_EXTENSION_OVERRIDES.get(mime_type.lower())
        if override:
            return override

        guessed = mimetypes.guess_extension(mime_type, strict=False)
        if guessed:
            return guessed

    if content_type == "voice":
        return ".ogg"
    if content_type == "photo":
        return ".jpg"
    if content_type == "video":
        return ".mp4"

    return ""


def _build_download_filename(
    *,
    content_type: str,
    message_id: int | None,
    original_file_name: str | None,
    telegram_file_path: str,
    mime_type: str | None,
) -> str:
    preferred_name = original_file_name or Path(telegram_file_path).name or content_type
    safe_stem = _sanitize_filename(Path(preferred_name).stem or content_type)
    extension = _pick_file_extension(
        content_type=content_type,
        original_file_name=original_file_name,
        telegram_file_path=telegram_file_path,
        mime_type=mime_type,
    )
    message_prefix = str(message_id) if message_id is not None else "attachment"
    return f"{message_prefix}_{safe_stem}{extension}"


def _resolve_send_method(attachment: OutboundAttachment) -> tuple[str, str]:
    """Return (telegram_api_method, file_field_name) for an outbound attachment."""
    send_as = attachment.send_as
    mime_type = attachment.mime_type or ""

    if send_as == "voice":
        return "sendVoice", "voice"
    if send_as == "audio":
        return "sendAudio", "audio"
    if send_as == "photo":
        return "sendPhoto", "photo"
    if send_as == "video":
        return "sendVideo", "video"

    if mime_type in _VOICE_MIME_TYPES:
        return "sendVoice", "voice"
    if mime_type.startswith("audio/"):
        return "sendAudio", "audio"
    if any(mime_type.startswith(prefix) for prefix in _PHOTO_MIME_PREFIXES):
        return "sendPhoto", "photo"
    if mime_type.startswith("video/"):
        return "sendVideo", "video"

    return "sendDocument", "document"


def _resolve_attachment_path(attachment: OutboundAttachment) -> Path:
    """Resolve an outbound attachment path relative to the workspace root."""
    raw_path = Path(attachment.path)
    if raw_path.is_absolute():
        return raw_path
    return settings.WORKSPACE_ROOT / raw_path


def _is_transient_telegram_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True

    if not isinstance(exc, TelegramAPIError):
        return False

    error_code = str(exc.error_code).upper()
    description = str(exc.description).lower()
    return error_code == "HTTP_ERROR" or "network error" in description or "timeout" in description


def _render_attachment_text(
    *,
    content_type: str,
    caption_text: str,
    workspace_path: str | None,
    original_file_name: str | None,
    mime_type: str | None,
    duration_seconds: int | None,
    download_failed: bool,
) -> str:
    attachment_label = _ATTACHMENT_LABELS.get(content_type, content_type)
    lines: list[str] = []

    if caption_text:
        lines.append(caption_text)

    if download_failed:
        lines.append(
            f"Telegram {attachment_label} received, but saving it to the workspace downloads directory failed."
        )
    elif workspace_path is not None:
        lines.append(f"Telegram {attachment_label} saved to workspace path: {workspace_path}.")
    else:
        lines.append(f"Telegram {attachment_label} received.")

    if original_file_name:
        lines.append(f"Original filename: {original_file_name}.")
    if mime_type:
        lines.append(f"MIME type: {mime_type}.")
    if duration_seconds is not None:
        lines.append(f"Duration: {duration_seconds} seconds.")

    return "\n".join(lines)


class TelegramChannelSettings(BaseSettings):
    bot_token: str
    poll_timeout_seconds: int = 30
    poll_limit: int = 100
    allowed_updates: Annotated[tuple[str, ...], NoDecode] = _DEFAULT_ALLOWED_UPDATES
    allowed_chat_ids: Annotated[list[int] | None, NoDecode] = None
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

    @field_validator("allowed_chat_ids", mode="before")
    @classmethod
    def _parse_allowed_chat_ids(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            parsed_chat_ids = _parse_chat_ids(value)
            return parsed_chat_ids or None
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
    destination_kind = "chat_id"

    def __init__(self, settings: TelegramChannelSettings | None = None) -> None:
        self._settings = settings or TelegramChannelSettings.from_env()
        self._client = AsyncClient(self._settings.bot_token)
        self._offset = 0
        self._allowed_chat_ids = frozenset(self._settings.allowed_chat_ids or ())
        self._failure_logger = ThrottledExceptionLogger(
            subject="Telegram",
            is_transient=_is_transient_telegram_error,
            cooldown_seconds=_TRANSIENT_TELEGRAM_LOG_COOLDOWN_SECONDS,
        )

    def instruction_context(self) -> dict[str, object]:
        chat_ids = sorted(self._allowed_chat_ids) if self._allowed_chat_ids else []
        return {
            "chat_id_example": str(chat_ids[0]) if chat_ids else "<chat_id>",
        }

    async def listen(self) -> AsyncIterator[InboundMessage]:
        if self._settings.delete_webhook_on_start:
            await self._delete_webhook()

        await self._configure_bot_commands()

        logger.info(
            "Starting Telegram channel polling "
            f"allowed_updates={list(self._settings.allowed_updates)} "
            f"allowed_chat_ids={sorted(self._allowed_chat_ids) if self._allowed_chat_ids else 'all'} "
            f"timeout={self._settings.poll_timeout_seconds}s"
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
                    # slight delay to prevent tight loop if Telegram returns updates immediately
                    await asyncio.sleep(0.1)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    continue
                except Exception as exc:
                    self._log_telegram_failure(action="polling failed", exc=exc, suppression_key="polling")
                    await asyncio.sleep(1)
                    continue

                if not isinstance(updates, list):
                    continue

                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        self._offset = update_id + 1

                    event = normalize(update)
                    inbound_message = await self._handle_event(event)
                    if inbound_message is not None:
                        yield inbound_message
        finally:
            await self.close()

    async def send_message(
        self,
        conversation_id: str,
        message_text: str,
        metadata: dict[str, object] | None = None,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        del metadata
        try:
            chat_id = int(conversation_id)
        except ValueError as exc:
            raise ValueError(f"Telegram conversation_id must be an integer chat ID, got: {conversation_id}") from exc

        if message_text.strip():
            await self._send_text(chat_id=chat_id, response_text=message_text)
        if attachments:
            await self._send_attachments(chat_id=chat_id, attachments=attachments)

    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        chat_id = self._chat_id_from_inbound_message(inbound_message)
        if chat_id is None:
            raise ValueError(
                "Telegram inbound message metadata is missing telegram_chat_id and conversation_id is not a valid chat ID"
            )

        reply_to_message_id = _coerce_int(inbound_message.metadata.get("telegram_message_id"))
        await self._send_text(chat_id=chat_id, response_text=response_text, reply_to_message_id=reply_to_message_id)
        if attachments:
            await self._send_attachments(
                chat_id=chat_id,
                attachments=attachments,
                reply_to_message_id=reply_to_message_id if isinstance(reply_to_message_id, int) else None,
            )

    @asynccontextmanager
    async def response_presence(self, inbound_message: InboundMessage) -> AsyncIterator[None]:
        chat_id = self._chat_id_from_inbound_message(inbound_message)
        if chat_id is None:
            yield
            return

        typing_task = asyncio.create_task(
            self._send_typing_presence(chat_id),
            name=f"telegram-typing:{chat_id}",
        )
        try:
            yield
        finally:
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task

    async def close(self) -> None:
        await self._client.close()

    async def _send_text(
        self,
        *,
        chat_id: int,
        response_text: str,
        reply_to_message_id: object | None = None,
    ) -> None:
        for index, chunk in enumerate(_chunk_message(response_text)):
            params = {
                "chat_id": chat_id,
                "text": chunk,
            }
            if index == 0 and self._settings.reply_to_message and isinstance(reply_to_message_id, int):
                params["reply_to_message_id"] = reply_to_message_id
                params["allow_sending_without_reply"] = True

            await self._client.call_async("sendMessage", **params)

    async def _send_file_multipart(
        self,
        *,
        method: str,
        chat_id: int,
        field_name: str,
        file_path: Path,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        url = f"{self._client.base_url}/{method}"
        http_client = await self._client._get_client()
        data: dict[str, str] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        if reply_to_message_id is not None and self._settings.reply_to_message:
            data["reply_to_message_id"] = str(reply_to_message_id)
            data["allow_sending_without_reply"] = "true"

        file_content = await asyncio.to_thread(file_path.read_bytes)
        file_name = file_path.name
        mime_type = mimetypes.guess_type(file_name)[0]
        files = {field_name: (file_name, file_content, mime_type)}

        response = await http_client.post(
            url,
            data=data,
            files=files,
            timeout=_TELEGRAM_FILE_UPLOAD_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

    async def _send_attachments(
        self,
        *,
        chat_id: int,
        attachments: tuple[OutboundAttachment, ...],
        reply_to_message_id: int | None = None,
    ) -> None:
        for index, attachment in enumerate(attachments):
            file_path = _resolve_attachment_path(attachment)
            if not await asyncio.to_thread(file_path.is_file):
                logger.warning(f"Skipping outbound attachment — file not found: {file_path}")
                continue

            method, field_name = _resolve_send_method(attachment)
            try:
                await self._send_file_multipart(
                    method=method,
                    chat_id=chat_id,
                    field_name=field_name,
                    file_path=file_path,
                    caption=attachment.display_name,
                    reply_to_message_id=reply_to_message_id if index == 0 else None,
                )
                logger.info(f"Sent Telegram attachment chat_id={chat_id} method={method} path={file_path.name}")
            except Exception as exc:
                self._log_telegram_failure(
                    action="attachment send failed",
                    exc=exc,
                    chat_id=chat_id,
                    suppression_key=f"send_attachment:{chat_id}:{file_path.name}",
                )

    async def _delete_webhook(self) -> None:
        await self._client.call_async(
            "deleteWebhook",
            drop_pending_updates=self._settings.drop_pending_updates,
        )
        logger.info(
            "Deleted Telegram webhook before starting long polling "
            f"drop_pending_updates={self._settings.drop_pending_updates}"
        )

    async def _configure_bot_commands(self) -> None:
        try:
            await self._client.call_async("setMyCommands", commands=list(_BOT_COMMANDS))
        except Exception as exc:
            self._log_telegram_failure(
                action="bot command configuration failed",
                exc=exc,
                suppression_key="configure_bot_commands",
            )
            return

        logger.info("Configured Telegram bot commands: /start, /clear")

    async def _handle_event(self, event: Event | None) -> InboundMessage | None:
        if event is None or event.type not in _TEXT_EVENT_TYPES:
            return None
        if event.chat_id == 0:
            return None
        if self._allowed_chat_ids and event.chat_id not in self._allowed_chat_ids:
            logger.warning(
                "Ignoring Telegram update from unauthorized chat "
                f"chat_id={event.chat_id} user_id={event.user_id} event_type={event.type}"
            )
            return None

        register_channel_conversation(self.name, str(event.chat_id))

        if event.type == "command" and event.name.lower() == "start":
            await self._send_text(chat_id=event.chat_id, response_text="ok", reply_to_message_id=event.message_id)
            return None

        return await self._build_inbound_message(event)

    async def _set_typing(self, chat_id: int) -> None:
        try:
            await self._client.call_async("sendChatAction", chat_id=chat_id, action="typing")
        except Exception as exc:
            self._log_telegram_failure(
                action="typing status failed",
                exc=exc,
                chat_id=chat_id,
                suppression_key=f"typing:{chat_id}",
            )

    async def _send_typing_presence(self, chat_id: int) -> None:
        for attempt in range(_MAX_TYPING_ACTIONS):
            await self._set_typing(chat_id)
            if attempt == _MAX_TYPING_ACTIONS - 1:
                return

            await asyncio.sleep(_TYPING_INTERVAL_SECONDS)

    async def _build_inbound_message(self, event: Event) -> InboundMessage | None:
        caption_text = self._caption_text(event)
        attachment_context = await self._download_attachment(event)
        message_text = event.text.strip() or caption_text

        metadata = {
            "source": "telegram",
            "telegram_chat_id": event.chat_id,
            "telegram_message_id": event.message_id,
            "telegram_update_id": event.raw.get("update_id"),
            "telegram_event_type": event.type,
            "telegram_chat_type": event.chat_type,
            "telegram_content_type": event.content_type,
            "telegram_username": event.username,
            "telegram_first_name": event.first_name,
        }

        if attachment_context is not None:
            attachment_text = attachment_context.get("text")
            attachment_metadata = attachment_context.get("metadata")
            if isinstance(attachment_text, str):
                message_text = attachment_text
            if isinstance(attachment_metadata, dict):
                metadata.update(attachment_metadata)

        if not message_text:
            return None

        return InboundMessage(
            channel_name=self.name,
            conversation_id=str(event.chat_id),
            user_id=str(event.user_id) if event.user_id else None,
            text=message_text,
            metadata=metadata,
        )

    def _caption_text(self, event: Event) -> str:
        message_payload = _message_payload_from_event(event)
        if message_payload is None:
            return ""

        caption = message_payload.get("caption")
        return caption.strip() if isinstance(caption, str) else ""

    async def _download_attachment(self, event: Event) -> dict[str, object] | None:
        message_payload = _message_payload_from_event(event)
        if message_payload is None:
            return None

        attachment_entry = _extract_attachment_payload(message_payload)
        if attachment_entry is None:
            return None

        content_type, attachment_payload = attachment_entry
        file_id = attachment_payload.get("file_id")
        original_file_name = attachment_payload.get("file_name")
        mime_type = attachment_payload.get("mime_type")
        duration_seconds = attachment_payload.get("duration")

        normalized_file_name = original_file_name if isinstance(original_file_name, str) else None
        normalized_mime_type = mime_type if isinstance(mime_type, str) else None
        normalized_duration = duration_seconds if isinstance(duration_seconds, int) else None
        caption_text = self._caption_text(event)
        base_metadata = {
            "telegram_attachment_type": content_type,
            "telegram_attachment_caption": caption_text or None,
            "telegram_attachment_original_file_name": normalized_file_name,
            "telegram_attachment_mime_type": normalized_mime_type,
            "telegram_attachment_duration_seconds": normalized_duration,
        }

        if not isinstance(file_id, str) or not file_id.strip():
            return {
                "text": _render_attachment_text(
                    content_type=content_type,
                    caption_text=caption_text,
                    workspace_path=None,
                    original_file_name=normalized_file_name,
                    mime_type=normalized_mime_type,
                    duration_seconds=normalized_duration,
                    download_failed=True,
                ),
                "metadata": {
                    **base_metadata,
                    "telegram_attachment_download_error": "missing file_id",
                },
            }

        try:
            file_info = await self._client.call_async("getFile", file_id=file_id)
            telegram_file_path = file_info.get("file_path") if isinstance(file_info, dict) else None
            if not isinstance(telegram_file_path, str) or not telegram_file_path:
                raise ValueError("Telegram getFile response did not include file_path")

            target_path = self._download_target_path(
                chat_id=event.chat_id,
                message_id=event.message_id,
                content_type=content_type,
                telegram_file_path=telegram_file_path,
                original_file_name=normalized_file_name,
                mime_type=normalized_mime_type,
            )
            await asyncio.to_thread(target_path.parent.mkdir, parents=True, exist_ok=True)

            http_client = await self._client._get_client()
            response = await http_client.get(self._file_download_url(telegram_file_path))
            response.raise_for_status()
            bytes_saved = await async_write_bytes_file(target_path, response.content)
            workspace_path = display_path(target_path, settings.WORKSPACE_ROOT)

            logger.info(
                f"Saved Telegram attachment chat_id={event.chat_id} message_id={event.message_id} path={workspace_path}"
            )

            return {
                "text": _render_attachment_text(
                    content_type=content_type,
                    caption_text=caption_text,
                    workspace_path=workspace_path,
                    original_file_name=normalized_file_name,
                    mime_type=normalized_mime_type,
                    duration_seconds=normalized_duration,
                    download_failed=False,
                ),
                "metadata": {
                    **base_metadata,
                    "inbound_attachments": [
                        {
                            "path": workspace_path,
                            "mime_type": normalized_mime_type,
                            "display_name": normalized_file_name,
                            "source": "telegram",
                            "kind": content_type,
                        }
                    ],
                    "telegram_attachment_download_path": workspace_path,
                    "telegram_attachment_bytes_saved": bytes_saved,
                },
            }
        except Exception as exc:
            self._log_telegram_failure(
                action="attachment download failed",
                exc=exc,
                chat_id=event.chat_id,
                message_id=event.message_id,
                suppression_key=f"attachment:{event.chat_id}:{event.message_id}",
            )
            return {
                "text": _render_attachment_text(
                    content_type=content_type,
                    caption_text=caption_text,
                    workspace_path=None,
                    original_file_name=normalized_file_name,
                    mime_type=normalized_mime_type,
                    duration_seconds=normalized_duration,
                    download_failed=True,
                ),
                "metadata": {
                    **base_metadata,
                    "telegram_attachment_download_error": str(exc),
                },
            }

    def _log_telegram_failure(
        self,
        *,
        action: str,
        exc: BaseException,
        chat_id: int | None = None,
        message_id: int | None = None,
        suppression_key: str,
    ) -> None:
        context: dict[str, object] = {}
        if chat_id is not None:
            context["chat_id"] = chat_id
        if message_id is not None:
            context["message_id"] = message_id

        self._failure_logger.log(
            action=action,
            exc=exc,
            suppression_key=suppression_key,
            context=context,
        )

    def _download_target_path(
        self,
        *,
        chat_id: int,
        message_id: int | None,
        content_type: str,
        telegram_file_path: str,
        original_file_name: str | None,
        mime_type: str | None,
    ) -> Path:
        file_name = _build_download_filename(
            content_type=content_type,
            message_id=message_id,
            original_file_name=original_file_name,
            telegram_file_path=telegram_file_path,
            mime_type=mime_type,
        )
        return _TELEGRAM_DOWNLOADS_DIR / str(chat_id) / file_name

    def _file_download_url(self, telegram_file_path: str) -> str:
        return f"https://api.telegram.org/file/bot{self._settings.bot_token}/{telegram_file_path}"

    def _chat_id_from_inbound_message(self, inbound_message: InboundMessage) -> int | None:
        chat_id = _coerce_int(inbound_message.metadata.get("telegram_chat_id"))
        if chat_id is not None:
            return chat_id

        return _coerce_int(inbound_message.conversation_id)


def create_channel() -> TelegramChannel:
    return TelegramChannel()
