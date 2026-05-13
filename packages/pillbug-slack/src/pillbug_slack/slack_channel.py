"""Slack channel plugin for Pillbug using slack-sdk Socket Mode."""

import asyncio
import mimetypes
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import aiohttp
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from app.core.config import settings
from app.core.log import ThrottledExceptionLogger, logger
from app.runtime.channels import BaseChannel, register_channel_conversation
from app.schema.messages import InboundMessage, OutboundAttachment
from app.util.workspace import async_write_bytes_file, display_path

_MAX_SLACK_MESSAGE_CHARS = 3500
_SLACK_DOWNLOADS_DIR = settings.WORKSPACE_ROOT / "downloads" / "slack"
_FILENAME_SANITIZER = re.compile(r"[^A-Za-z0-9._-]+")
_MENTION_PATTERN = re.compile(r"<@[UW][A-Z0-9]+>")
_TRANSIENT_SLACK_LOG_COOLDOWN_SECONDS = 60.0
_INTERESTING_EVENT_TYPES = frozenset({"message", "app_mention"})
_IGNORED_MESSAGE_SUBTYPES = frozenset(
    {
        "bot_message",
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
    }
)
_SLACK_QUEUE_SENTINEL: Any = object()


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _chunk_message(text: str, *, max_chars: int = _MAX_SLACK_MESSAGE_CHARS) -> tuple[str, ...]:
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


def _sanitize_filename(value: str) -> str:
    sanitized = _FILENAME_SANITIZER.sub("_", value).strip("._")
    return sanitized or "attachment"


def _is_transient_slack_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError | ConnectionError | OSError | aiohttp.ClientError):
        return True

    if isinstance(exc, SlackApiError):
        error_code = (exc.response.get("error") if exc.response else "") or ""
        return error_code in {"ratelimited", "service_unavailable", "fatal_error", "timeout"}

    return False


def _resolve_attachment_path(attachment: OutboundAttachment) -> Path:
    raw_path = Path(attachment.path)
    if raw_path.is_absolute():
        return raw_path
    return settings.WORKSPACE_ROOT / raw_path


def _strip_mentions(text: str) -> str:
    return _MENTION_PATTERN.sub("", text).strip()


def _parse_conversation_id(conversation_id: str) -> tuple[str, str | None]:
    """Split conversation_id into (channel_id, thread_ts)."""
    channel_id, separator, thread_ts = conversation_id.partition(":")
    channel_id = channel_id.strip()
    if not channel_id:
        raise ValueError("Slack conversation_id must include a non-empty channel id")

    if not separator:
        return channel_id, None

    normalized_thread_ts = thread_ts.strip()
    return channel_id, normalized_thread_ts or None


def _build_conversation_id(channel_id: str, thread_ts: str | None) -> str:
    if thread_ts:
        return f"{channel_id}:{thread_ts}"
    return channel_id


def _render_attachment_text(
    *,
    caption_text: str,
    workspace_path: str | None,
    original_file_name: str | None,
    mime_type: str | None,
    download_failed: bool,
) -> str:
    lines: list[str] = []

    if caption_text:
        lines.append(caption_text)

    if download_failed:
        lines.append("Slack file received, but saving it to the workspace downloads directory failed.")
    elif workspace_path is not None:
        lines.append(f"Slack file saved to workspace path: {workspace_path}.")
    else:
        lines.append("Slack file received.")

    if original_file_name:
        lines.append(f"Original filename: {original_file_name}.")
    if mime_type:
        lines.append(f"MIME type: {mime_type}.")

    return "\n".join(lines)


class SlackChannelSettings(BaseSettings):
    app_token: SecretStr
    bot_token: SecretStr
    allowed_channel_ids: Annotated[list[str] | None, NoDecode] = None
    reply_in_thread: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PB_SLACK_",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("app_token")
    @classmethod
    def _validate_app_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if not token:
            raise ValueError("PB_SLACK_APP_TOKEN is required when the Slack channel is enabled")
        if not token.startswith("xapp-"):
            raise ValueError("PB_SLACK_APP_TOKEN must be an app-level token starting with 'xapp-'")
        return SecretStr(token)

    @field_validator("bot_token")
    @classmethod
    def _validate_bot_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if not token:
            raise ValueError("PB_SLACK_BOT_TOKEN is required when the Slack channel is enabled")
        if not token.startswith("xoxb-"):
            raise ValueError("PB_SLACK_BOT_TOKEN must be a bot user token starting with 'xoxb-'")
        return SecretStr(token)

    @field_validator("allowed_channel_ids", mode="before")
    @classmethod
    def _parse_allowed_channel_ids(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            parsed = _split_csv(value)
            return parsed or None
        return value

    @classmethod
    def from_env(cls) -> SlackChannelSettings:
        return cls()  # type: ignore[call-arg]


class SlackChannel(BaseChannel):
    name = "slack"
    destination_kind = "channel_id"

    def __init__(self, settings: SlackChannelSettings | None = None) -> None:
        self._settings = settings or SlackChannelSettings.from_env()
        self._web_client = AsyncWebClient(token=self._settings.bot_token.get_secret_value())
        self._socket_client = SocketModeClient(
            app_token=self._settings.app_token.get_secret_value(),
            web_client=self._web_client,
        )
        self._allowed_channel_ids = frozenset(self._settings.allowed_channel_ids or ())
        self._failure_logger = ThrottledExceptionLogger(
            subject="Slack",
            is_transient=_is_transient_slack_error,
            cooldown_seconds=_TRANSIENT_SLACK_LOG_COOLDOWN_SECONDS,
        )
        self._bot_user_id: str | None = None
        self._event_queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue()
        self._listener_registered = False
        self._closed = False

    def instruction_context(self) -> dict[str, object]:
        channel_ids = sorted(self._allowed_channel_ids) if self._allowed_channel_ids else []
        return {
            "channel_id_example": channel_ids[0] if channel_ids else "<channel_id>",
        }

    async def listen(self) -> AsyncIterator[InboundMessage]:
        await self._ensure_started()

        logger.info(
            "Started Slack channel "
            f"bot_user_id={self._bot_user_id or 'unknown'} "
            f"allowed_channel_ids={sorted(self._allowed_channel_ids) if self._allowed_channel_ids else 'all'} "
            f"reply_in_thread={self._settings.reply_in_thread}"
        )

        try:
            while True:
                event_payload = await self._event_queue.get()
                if event_payload is _SLACK_QUEUE_SENTINEL:
                    return

                if not isinstance(event_payload, dict):
                    continue

                inbound_message = await self._handle_event(event_payload)
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
        channel_id, thread_ts = _parse_conversation_id(conversation_id)

        if message_text.strip():
            await self._send_text(channel_id=channel_id, text=message_text, thread_ts=thread_ts)
        if attachments:
            await self._send_attachments(channel_id=channel_id, thread_ts=thread_ts, attachments=attachments)

    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        channel_id = self._channel_id_from_inbound(inbound_message)
        if not channel_id:
            raise ValueError("Cannot determine Slack channel_id from inbound message")

        thread_ts: str | None = None
        if self._settings.reply_in_thread:
            thread_ts = self._thread_ts_from_inbound(inbound_message)

        await self._send_text(channel_id=channel_id, text=response_text, thread_ts=thread_ts)
        if attachments:
            await self._send_attachments(channel_id=channel_id, thread_ts=thread_ts, attachments=attachments)

    @asynccontextmanager
    async def response_presence(self, inbound_message: InboundMessage) -> AsyncIterator[None]:
        del inbound_message
        yield

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        try:
            await self._socket_client.disconnect()
        except Exception as exc:
            self._log_slack_failure(action="disconnect failed", exc=exc, suppression_key="disconnect")

        try:
            await self._socket_client.close()
        except Exception as exc:
            self._log_slack_failure(action="close failed", exc=exc, suppression_key="close")

        try:
            await self._event_queue.put(_SLACK_QUEUE_SENTINEL)
        except Exception:
            pass

    # --- Internal helpers ---

    async def _ensure_started(self) -> None:
        if self._listener_registered:
            return

        try:
            auth_response = await self._web_client.auth_test()
            user_id = auth_response.get("user_id")
            if isinstance(user_id, str):
                self._bot_user_id = user_id
            team = auth_response.get("team")
            logger.info(f"Slack auth ok bot_user_id={self._bot_user_id} team={team}")
        except Exception as exc:
            self._log_slack_failure(action="auth.test failed", exc=exc, suppression_key="auth_test")
            raise

        self._socket_client.socket_mode_request_listeners.append(self._on_socket_request)
        self._listener_registered = True

        await self._socket_client.connect()

    async def _on_socket_request(
        self,
        client: SocketModeClient,
        request: SocketModeRequest,
    ) -> None:
        try:
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        except Exception as exc:
            self._log_slack_failure(action="ack failed", exc=exc, suppression_key="ack")

        if request.type != "events_api":
            return

        try:
            payload = request.payload or {}
            event = payload.get("event") if isinstance(payload, dict) else None
            if not isinstance(event, dict):
                return

            event_type = event.get("type")
            if event_type not in _INTERESTING_EVENT_TYPES:
                return

            await self._event_queue.put(event)
        except Exception as exc:
            self._log_slack_failure(action="queue event failed", exc=exc, suppression_key="queue")

    async def _handle_event(self, event: dict[str, Any]) -> InboundMessage | None:
        event_type = event.get("type")
        subtype = event.get("subtype")

        if subtype in _IGNORED_MESSAGE_SUBTYPES:
            return None
        if event.get("hidden") is True:
            return None
        if event.get("bot_id"):
            return None

        user_id = event.get("user")
        if isinstance(user_id, str) and self._bot_user_id and user_id == self._bot_user_id:
            return None
        if not isinstance(user_id, str) or not user_id.strip():
            return None

        channel_id = event.get("channel")
        if not isinstance(channel_id, str) or not channel_id.strip():
            return None
        if self._allowed_channel_ids and channel_id not in self._allowed_channel_ids:
            logger.warning(
                f"Ignoring Slack event from unauthorized channel channel_id={channel_id} user_id={user_id} type={event_type}"
            )
            return None

        channel_type = event.get("channel_type")
        is_dm = channel_type == "im"

        # In public/private channels we only act on app_mention or thread replies; ignore broadcast chatter.
        if not is_dm and event_type == "message":
            if not event.get("thread_ts"):
                return None

        message_ts = event.get("ts")
        thread_ts = event.get("thread_ts") or message_ts
        if not isinstance(message_ts, str):
            return None

        raw_text = event.get("text") if isinstance(event.get("text"), str) else ""
        message_text = _strip_mentions(raw_text)

        conversation_id = channel_id if is_dm else _build_conversation_id(channel_id, thread_ts)
        register_channel_conversation(self.name, conversation_id)

        metadata: dict[str, Any] = {
            "source": "slack",
            "slack_channel_id": channel_id,
            "slack_channel_type": channel_type,
            "slack_message_ts": message_ts,
            "slack_thread_ts": thread_ts if not is_dm else None,
            "slack_event_type": event_type,
            "slack_team": event.get("team"),
            "slack_user_id": user_id,
        }

        files = event.get("files")
        if isinstance(files, list) and files:
            attachment_context = await self._download_files(channel_id=channel_id, message_ts=message_ts, files=files)
            if attachment_context is not None:
                attachment_text = attachment_context.get("text")
                attachment_metadata = attachment_context.get("metadata")
                if isinstance(attachment_text, str) and attachment_text:
                    message_text = f"{message_text}\n{attachment_text}".strip() if message_text else attachment_text
                if isinstance(attachment_metadata, dict):
                    metadata.update(attachment_metadata)

        if not message_text:
            return None

        return InboundMessage(
            channel_name=self.name,
            conversation_id=conversation_id,
            user_id=user_id,
            text=message_text,
            metadata=metadata,
        )

    async def _send_text(
        self,
        *,
        channel_id: str,
        text: str,
        thread_ts: str | None,
    ) -> None:
        chunks = _chunk_message(text)
        for chunk in chunks:
            params: dict[str, Any] = {"channel": channel_id, "text": chunk}
            if thread_ts:
                params["thread_ts"] = thread_ts

            try:
                await self._web_client.chat_postMessage(**params)
            except Exception as exc:
                self._log_slack_failure(
                    action="chat_postMessage failed",
                    exc=exc,
                    channel_id=channel_id,
                    suppression_key=f"send_text:{channel_id}",
                )
                return

    async def _send_attachments(
        self,
        *,
        channel_id: str,
        thread_ts: str | None,
        attachments: tuple[OutboundAttachment, ...],
    ) -> None:
        for attachment in attachments:
            file_path = _resolve_attachment_path(attachment)
            if not await asyncio.to_thread(file_path.is_file):
                logger.warning(f"Skipping outbound attachment — file not found: {file_path}")
                continue

            try:
                upload_params: dict[str, Any] = {
                    "channel": channel_id,
                    "file": str(file_path),
                    "filename": file_path.name,
                }
                if attachment.display_name:
                    upload_params["title"] = attachment.display_name
                if thread_ts:
                    upload_params["thread_ts"] = thread_ts

                await self._web_client.files_upload_v2(**upload_params)
                logger.info(f"Sent Slack attachment channel_id={channel_id} path={file_path.name}")
            except Exception as exc:
                self._log_slack_failure(
                    action="attachment upload failed",
                    exc=exc,
                    channel_id=channel_id,
                    suppression_key=f"send_attachment:{channel_id}:{file_path.name}",
                )

    async def _download_files(
        self,
        *,
        channel_id: str,
        message_ts: str,
        files: list[Any],
    ) -> dict[str, object] | None:
        download_entries: list[dict[str, object]] = []
        rendered_lines: list[str] = []
        any_downloaded = False
        any_failed = False
        bot_token = self._settings.bot_token.get_secret_value()
        target_dir = _SLACK_DOWNLOADS_DIR / _sanitize_filename(channel_id)
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)

        async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {bot_token}"}) as session:
            for file_payload in files:
                if not isinstance(file_payload, dict):
                    continue

                file_id = file_payload.get("id")
                url = file_payload.get("url_private_download") or file_payload.get("url_private")
                original_file_name = file_payload.get("name")
                mime_type = file_payload.get("mimetype")
                file_mode = file_payload.get("mode")

                normalized_file_name = original_file_name if isinstance(original_file_name, str) else None
                normalized_mime_type = mime_type if isinstance(mime_type, str) else None

                if file_mode in {"hidden_by_limit", "tombstone"}:
                    logger.info(f"Skipping Slack file with mode={file_mode} file_id={file_id}")
                    continue

                if not isinstance(url, str) or not url.startswith("http"):
                    any_failed = True
                    rendered_lines.append(
                        _render_attachment_text(
                            caption_text="",
                            workspace_path=None,
                            original_file_name=normalized_file_name,
                            mime_type=normalized_mime_type,
                            download_failed=True,
                        )
                    )
                    continue

                try:
                    async with session.get(url) as response:
                        response.raise_for_status()
                        file_bytes = await response.read()

                    safe_message_ts = _sanitize_filename(message_ts)
                    safe_name = _sanitize_filename(normalized_file_name or str(file_id or "attachment"))
                    target_path = target_dir / f"{safe_message_ts}_{safe_name}"
                    if not target_path.suffix and normalized_mime_type:
                        guessed = mimetypes.guess_extension(normalized_mime_type, strict=False)
                        if guessed:
                            target_path = target_path.with_suffix(guessed)

                    bytes_saved = await async_write_bytes_file(target_path, file_bytes)
                    workspace_path = display_path(target_path, settings.WORKSPACE_ROOT)
                    any_downloaded = True

                    logger.info(
                        f"Saved Slack attachment channel_id={channel_id} message_ts={message_ts} path={workspace_path}"
                    )

                    download_entries.append(
                        {
                            "path": workspace_path,
                            "mime_type": normalized_mime_type,
                            "display_name": normalized_file_name,
                            "source": "slack",
                            "kind": "file",
                        }
                    )
                    rendered_lines.append(
                        _render_attachment_text(
                            caption_text="",
                            workspace_path=workspace_path,
                            original_file_name=normalized_file_name,
                            mime_type=normalized_mime_type,
                            download_failed=False,
                        )
                    )
                    download_entries[-1]["bytes_saved"] = bytes_saved
                except Exception as exc:
                    any_failed = True
                    self._log_slack_failure(
                        action="attachment download failed",
                        exc=exc,
                        channel_id=channel_id,
                        suppression_key=f"attachment:{channel_id}:{file_id}",
                    )
                    rendered_lines.append(
                        _render_attachment_text(
                            caption_text="",
                            workspace_path=None,
                            original_file_name=normalized_file_name,
                            mime_type=normalized_mime_type,
                            download_failed=True,
                        )
                    )

        if not rendered_lines:
            return None

        metadata: dict[str, object] = {}
        if download_entries:
            metadata["inbound_attachments"] = download_entries
        metadata["slack_attachment_download_failed"] = any_failed and not any_downloaded

        return {
            "text": "\n".join(rendered_lines),
            "metadata": metadata,
        }

    def _channel_id_from_inbound(self, inbound_message: InboundMessage) -> str | None:
        metadata_channel = inbound_message.metadata.get("slack_channel_id")
        if isinstance(metadata_channel, str) and metadata_channel.strip():
            return metadata_channel.strip()

        try:
            channel_id, _thread_ts = _parse_conversation_id(inbound_message.conversation_id)
        except ValueError:
            return None
        return channel_id

    def _thread_ts_from_inbound(self, inbound_message: InboundMessage) -> str | None:
        metadata_thread = inbound_message.metadata.get("slack_thread_ts")
        if isinstance(metadata_thread, str) and metadata_thread.strip():
            return metadata_thread.strip()

        try:
            _channel_id, thread_ts = _parse_conversation_id(inbound_message.conversation_id)
        except ValueError:
            return None
        return thread_ts

    def _log_slack_failure(
        self,
        *,
        action: str,
        exc: BaseException,
        channel_id: str | None = None,
        suppression_key: str,
    ) -> None:
        context: dict[str, object] = {}
        if channel_id is not None:
            context["channel_id"] = channel_id

        self._failure_logger.log(
            action=action,
            exc=exc,
            suppression_key=suppression_key,
            context=context,
        )


def create_channel() -> SlackChannel:
    return SlackChannel()
