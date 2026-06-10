"""Matrix channel plugin for Pillbug using matrix-nio."""

import asyncio
import mimetypes
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated

from markdown_it import MarkdownIt
from mutagen import File as MutagenFile
from nio import (
    AsyncClient,
    RoomMessageAudio,
    RoomMessageFile,
    RoomMessageImage,
    RoomMessageText,
    RoomMessageVideo,
    RoomRedactError,
    RoomSendError,
    SyncError,
    SyncResponse,
    UploadError,
)
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.config import settings
from app.core.log import ThrottledExceptionLogger, logger
from app.runtime.channel_helpers import (
    build_threaded_conversation_id,
    chunk_message,
    parse_threaded_conversation_id,
    render_attachment_text,
    resolve_attachment_path,
    sanitize_filename,
    split_csv,
)
from app.runtime.channels import BaseChannel, register_channel_conversation
from app.schema.messages import InboundMessage, OutboundAttachment
from app.util.workspace import async_write_bytes_file, display_path

_MAX_MESSAGE_CHARS = 4000
_MARKDOWN_RENDERER = MarkdownIt("commonmark", {"html": False, "breaks": True, "linkify": True}).enable("linkify")
_MATRIX_DOWNLOADS_DIR = settings.WORKSPACE_ROOT / "downloads" / "matrix"
_TRANSIENT_LOG_COOLDOWN_SECONDS = 60.0
_TYPING_INTERVAL_SECONDS = 25.0
_TYPING_TIMEOUT_MS = 30000
_MAX_TYPING_ACTIONS = 10
_PRESENCE_REACTION_KEY = "🤔"
_FILE_UPLOAD_TIMEOUT_SECONDS = 120.0

_MESSAGE_EVENT_TYPES = (RoomMessageText, RoomMessageImage, RoomMessageAudio, RoomMessageVideo, RoomMessageFile)
_ATTACHMENT_EVENT_TYPES = (RoomMessageImage, RoomMessageAudio, RoomMessageVideo, RoomMessageFile)

_ATTACHMENT_LABELS = {
    "image": "image",
    "audio": "audio file",
    "video": "video",
    "file": "file attachment",
}

_MSGTYPE_TO_KIND = {
    "m.image": "image",
    "m.audio": "audio",
    "m.video": "video",
    "m.file": "file",
}

_SEND_AS_TO_MSGTYPE = {
    "photo": "m.image",
    "image": "m.image",
    "audio": "m.audio",
    "voice": "m.audio",
    "video": "m.video",
    "document": "m.file",
}

_MIME_TO_MSGTYPE_PREFIX = {
    "image/": "m.image",
    "audio/": "m.audio",
    "video/": "m.video",
}


def _render_markdown_html(text: str) -> str:
    return _MARKDOWN_RENDERER.render(text).strip()


def _is_transient_matrix_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError | ConnectionError | OSError):
        return True

    error_message = str(exc).lower()
    return "timeout" in error_message or "connection" in error_message


# Matrix room IDs and v1/v2 event IDs both contain ':', so we cannot reuse Slack's colon
# separator. '|' is not part of any Matrix sigil grammar (`!`, `$`, `#`, `@`).
_CONVERSATION_ID_SEPARATOR = "|"


def _parse_conversation_id(conversation_id: str) -> tuple[str, str | None]:
    """Split conversation_id into (room_id, thread_root_event_id)."""
    return parse_threaded_conversation_id(
        conversation_id,
        separator=_CONVERSATION_ID_SEPARATOR,
        empty_error="Matrix conversation_id (room_id) must not be empty",
    )


def _build_conversation_id(room_id: str, thread_root_event_id: str | None) -> str:
    return build_threaded_conversation_id(room_id, thread_root_event_id, separator=_CONVERSATION_ID_SEPARATOR)


def _extract_thread_root(event: object) -> str | None:
    """Return the thread root event_id when the event is part of an existing m.thread relation."""
    source = getattr(event, "source", None)
    if not isinstance(source, dict):
        return None
    content = source.get("content")
    if not isinstance(content, dict):
        return None
    relates_to = content.get("m.relates_to")
    if not isinstance(relates_to, dict):
        return None
    if relates_to.get("rel_type") != "m.thread":
        return None
    event_id = relates_to.get("event_id")
    if isinstance(event_id, str) and event_id.strip():
        return event_id.strip()
    return None


def _generate_mock_waveform(num_samples: int = 64) -> list[int]:
    return [random.randint(200, 900) for _ in range(num_samples)]


def _read_audio_duration_ms(file_path: Path) -> int | None:
    try:
        audio = MutagenFile(str(file_path))
    except Exception:
        return None
    if audio is None or audio.info is None:
        return None
    length = getattr(audio.info, "length", None)
    if not length or length <= 0:
        return None
    return int(round(length * 1000))


def _resolve_msgtype(attachment: OutboundAttachment) -> str:
    if attachment.send_as:
        msgtype = _SEND_AS_TO_MSGTYPE.get(attachment.send_as)
        if msgtype:
            return msgtype

    mime_type = attachment.mime_type or ""
    for prefix, msgtype in _MIME_TO_MSGTYPE_PREFIX.items():
        if mime_type.startswith(prefix):
            return msgtype

    return "m.file"


def _render_attachment_text(
    *,
    kind: str,
    caption_text: str,
    workspace_path: str | None,
    original_file_name: str | None,
    mime_type: str | None,
    download_failed: bool,
) -> str:
    return render_attachment_text(
        channel_label="Matrix",
        attachment_label=_ATTACHMENT_LABELS.get(kind, kind),
        caption_text=caption_text,
        workspace_path=workspace_path,
        original_file_name=original_file_name,
        mime_type=mime_type,
        download_failed=download_failed,
    )


class MatrixChannelSettings(BaseSettings):
    homeserver_url: str
    access_token: SecretStr
    user_id: str
    device_id: str | None = None
    allowed_room_ids: Annotated[list[str] | None, NoDecode] = None
    sync_timeout_ms: int = 30000
    reply_to_message: bool = True
    reply_in_thread: bool = False
    reaction_presence: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PB_MATRIX_",
        extra="ignore",
        case_sensitive=False,
    )

    @field_validator("homeserver_url")
    @classmethod
    def _validate_homeserver_url(cls, value: str) -> str:
        stripped = value.strip().rstrip("/")
        if not stripped:
            raise ValueError("PB_MATRIX_HOMESERVER_URL is required when the Matrix channel is enabled")
        return stripped

    @field_validator("user_id")
    @classmethod
    def _validate_user_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("PB_MATRIX_USER_ID is required when the Matrix channel is enabled")
        return stripped

    @field_validator("allowed_room_ids", mode="before")
    @classmethod
    def _parse_allowed_room_ids(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            parsed = split_csv(value)
            return parsed or None
        return value

    @field_validator("sync_timeout_ms")
    @classmethod
    def _validate_sync_timeout_ms(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("PB_MATRIX_SYNC_TIMEOUT_MS must be greater than zero")
        return value

    @classmethod
    def from_env(cls) -> MatrixChannelSettings:
        return cls()  # type: ignore[call-arg]


class MatrixChannel(BaseChannel):
    name = "matrix"
    destination_kind = "room_id"

    def __init__(self, settings: MatrixChannelSettings | None = None) -> None:
        self._settings = settings or MatrixChannelSettings.from_env()
        self._client = AsyncClient(
            self._settings.homeserver_url,
            self._settings.user_id,
        )
        self._client.access_token = self._settings.access_token.get_secret_value()
        if self._settings.device_id:
            self._client.device_id = self._settings.device_id
        self._allowed_room_ids = frozenset(self._settings.allowed_room_ids or ())
        self._failure_logger = ThrottledExceptionLogger(
            subject="Matrix",
            is_transient=_is_transient_matrix_error,
            cooldown_seconds=_TRANSIENT_LOG_COOLDOWN_SECONDS,
        )
        self._since_token: str | None = None

    def instruction_context(self) -> dict[str, object]:
        room_ids = sorted(self._allowed_room_ids) if self._allowed_room_ids else []
        return {
            "room_id_example": room_ids[0] if room_ids else "!room_id:homeserver",
        }

    async def listen(self) -> AsyncIterator[InboundMessage]:
        logger.info(
            "Starting Matrix channel sync "
            f"homeserver={self._settings.homeserver_url} "
            f"user_id={self._settings.user_id} "
            f"allowed_room_ids={sorted(self._allowed_room_ids) if self._allowed_room_ids else 'all'} "
            f"sync_timeout={self._settings.sync_timeout_ms}ms"
        )

        # Initial sync to get the since token — skip historical messages.
        try:
            initial_sync = await self._client.sync(timeout=0, full_state=False)
            if isinstance(initial_sync, SyncResponse):
                self._since_token = initial_sync.next_batch
                logger.info(f"Matrix initial sync complete next_batch={self._since_token}")
            elif isinstance(initial_sync, SyncError):
                logger.error(f"Matrix initial sync failed: {initial_sync.message}")
        except Exception as exc:
            self._log_matrix_failure(action="initial sync failed", exc=exc, suppression_key="initial_sync")

        try:
            while True:
                try:
                    sync_response = await self._client.sync(
                        timeout=self._settings.sync_timeout_ms,
                        since=self._since_token,
                        full_state=False,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._log_matrix_failure(action="sync failed", exc=exc, suppression_key="sync")
                    await asyncio.sleep(1)
                    continue

                if isinstance(sync_response, SyncError):
                    self._log_matrix_failure(
                        action="sync returned error",
                        exc=RuntimeError(sync_response.message),
                        suppression_key="sync",
                    )
                    await asyncio.sleep(1)
                    continue

                self._since_token = sync_response.next_batch

                for room_id, room_info in sync_response.rooms.join.items():
                    if self._allowed_room_ids and room_id not in self._allowed_room_ids:
                        continue

                    for event in room_info.timeline.events:
                        if event.sender == self._settings.user_id:
                            continue

                        inbound_message = await self._handle_room_event(room_id, event)
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
        room_id, thread_root_event_id = _parse_conversation_id(conversation_id)

        # Proactive sends (scheduled tasks, operator pushes) reuse a conversation_id whose
        # thread root was captured from an earlier inbound message. Appending to that stale
        # thread is wrong, so when threading is enabled we start a fresh thread rooted at this
        # message instead of honoring the embedded thread root.
        start_new_thread = self._settings.reply_in_thread
        new_thread_root: str | None = None

        if message_text.strip():
            new_thread_root = await self._send_text(
                room_id=room_id,
                text=message_text,
                thread_root_event_id=thread_root_event_id,
                reply_to_event_id=thread_root_event_id,
                start_new_thread=start_new_thread,
            )
        if attachments:
            await self._send_attachments(
                room_id=room_id,
                attachments=attachments,
                thread_root_event_id=new_thread_root if start_new_thread else thread_root_event_id,
                start_new_thread=start_new_thread and new_thread_root is None,
            )

    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        room_id = self._room_id_from_inbound(inbound_message)
        if not room_id:
            raise ValueError("Cannot determine Matrix room_id from inbound message")

        reply_to_event_id = str(inbound_message.metadata.get("matrix_event_id", "")).strip() or None
        thread_root_event_id = self._thread_root_from_inbound(inbound_message)

        await self._send_text(
            room_id=room_id,
            text=response_text,
            thread_root_event_id=thread_root_event_id,
            reply_to_event_id=reply_to_event_id,
        )
        if attachments:
            await self._send_attachments(
                room_id=room_id,
                attachments=attachments,
                thread_root_event_id=thread_root_event_id,
            )

    @asynccontextmanager
    async def response_presence(self, inbound_message: InboundMessage) -> AsyncIterator[None]:
        room_id = self._room_id_from_inbound(inbound_message)
        if not room_id:
            yield
            return

        if self._settings.reaction_presence:
            target_event_id = str(inbound_message.metadata.get("matrix_event_id", "")).strip() or None
            reaction_event_id = await self._send_reaction(room_id, target_event_id) if target_event_id else None
            try:
                yield
            finally:
                if reaction_event_id:
                    await self._redact_event(room_id, reaction_event_id)
            return

        typing_task = asyncio.create_task(
            self._send_typing_presence(room_id),
            name=f"matrix-typing:{room_id}",
        )
        try:
            yield
        finally:
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task

            try:
                await self._client.room_typing(room_id, typing_state=False)
            except Exception:
                pass

    async def close(self) -> None:
        await self._client.close()

    # --- Internal helpers ---

    async def _send_text(
        self,
        *,
        room_id: str,
        text: str,
        thread_root_event_id: str | None = None,
        reply_to_event_id: str | None = None,
        start_new_thread: bool = False,
    ) -> str | None:
        """Send ``text`` as one or more chunks, returning the first event_id sent.

        When ``start_new_thread`` is set the first chunk is sent top-level to root a new
        thread and later chunks thread under it, so a chunked proactive message stays in
        one self-contained thread instead of leaking onto the main timeline.
        """
        new_thread_root: str | None = None
        first_event_id: str | None = None
        for index, chunk in enumerate(chunk_message(text, max_chars=_MAX_MESSAGE_CHARS)):
            content: dict[str, object] = {
                "msgtype": "m.text",
                "body": chunk,
                "format": "org.matrix.custom.html",
                "formatted_body": _render_markdown_html(chunk),
            }

            if start_new_thread:
                relates_to = (
                    self._build_relates_to(
                        thread_root_event_id=new_thread_root,
                        reply_to_event_id=new_thread_root,
                    )
                    if new_thread_root
                    else None
                )
            elif index == 0:
                relates_to = self._build_relates_to(
                    thread_root_event_id=thread_root_event_id,
                    reply_to_event_id=reply_to_event_id,
                )
            else:
                relates_to = None

            if relates_to:
                content["m.relates_to"] = relates_to

            response = await self._client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
            )
            if isinstance(response, RoomSendError):
                logger.error(f"Matrix send failed room_id={room_id}: {response.message}")
                continue

            event_id = getattr(response, "event_id", None)
            if first_event_id is None:
                first_event_id = event_id
            if start_new_thread and new_thread_root is None and event_id:
                new_thread_root = event_id

        return first_event_id

    def _build_relates_to(
        self,
        *,
        thread_root_event_id: str | None,
        reply_to_event_id: str | None,
    ) -> dict[str, object] | None:
        if thread_root_event_id:
            in_reply_to_event_id = reply_to_event_id or thread_root_event_id
            return {
                "rel_type": "m.thread",
                "event_id": thread_root_event_id,
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": in_reply_to_event_id},
            }
        if reply_to_event_id and self._settings.reply_to_message:
            return {"m.in_reply_to": {"event_id": reply_to_event_id}}
        return None

    def _room_id_from_inbound(self, inbound_message: InboundMessage) -> str | None:
        metadata_room = inbound_message.metadata.get("matrix_room_id")
        if isinstance(metadata_room, str) and metadata_room.strip():
            return metadata_room.strip()
        try:
            room_id, _thread_root = _parse_conversation_id(inbound_message.conversation_id)
        except ValueError:
            return None
        return room_id

    def _thread_root_from_inbound(self, inbound_message: InboundMessage) -> str | None:
        metadata_root = inbound_message.metadata.get("matrix_thread_root_event_id")
        if isinstance(metadata_root, str) and metadata_root.strip():
            return metadata_root.strip()
        try:
            _room_id, thread_root = _parse_conversation_id(inbound_message.conversation_id)
        except ValueError:
            return None
        return thread_root

    async def _send_attachments(
        self,
        *,
        room_id: str,
        attachments: tuple[OutboundAttachment, ...],
        thread_root_event_id: str | None = None,
        start_new_thread: bool = False,
    ) -> None:
        # When ``start_new_thread`` is set the first attachment is sent top-level to root a
        # new thread, then ``thread_root`` is updated so the rest thread under it.
        thread_root = thread_root_event_id
        for attachment in attachments:
            file_path = resolve_attachment_path(attachment)
            if not await asyncio.to_thread(file_path.is_file):
                logger.warning(f"Skipping outbound attachment — file not found: {file_path}")
                continue

            try:
                file_size = (await asyncio.to_thread(file_path.stat)).st_size
                mime_type = (
                    attachment.mime_type or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
                )

                with file_path.open("rb") as file_handle:
                    upload_response, _maybe_keys = await self._client.upload(
                        data_provider=file_handle,
                        content_type=mime_type,
                        filename=file_path.name,
                        filesize=file_size,
                    )

                if isinstance(upload_response, UploadError):
                    logger.error(f"Matrix upload failed room_id={room_id}: {upload_response.message}")
                    continue

                content_uri = upload_response.content_uri
                is_voice_message = file_path.suffix.lower() == ".ogg"
                msgtype = "m.audio" if is_voice_message else _resolve_msgtype(attachment)

                info: dict[str, object] = {
                    "mimetype": mime_type,
                    "size": file_size,
                }
                duration_ms: int | None = None
                if is_voice_message:
                    duration_ms = await asyncio.to_thread(_read_audio_duration_ms, file_path)
                    if duration_ms is not None:
                        info["duration"] = duration_ms

                content: dict[str, object] = {
                    "msgtype": msgtype,
                    "body": attachment.display_name or file_path.name,
                    "url": content_uri,
                    "info": info,
                }

                if thread_root:
                    content["m.relates_to"] = {
                        "rel_type": "m.thread",
                        "event_id": thread_root,
                        "is_falling_back": True,
                        "m.in_reply_to": {"event_id": thread_root},
                    }

                if is_voice_message:
                    audio_block: dict[str, object] = {"waveform": _generate_mock_waveform()}
                    if duration_ms is not None:
                        audio_block["duration"] = duration_ms
                    content["org.matrix.msc1767.audio"] = audio_block
                    content["org.matrix.msc1767.file"] = {
                        "url": content_uri,
                        "name": attachment.display_name or file_path.name,
                        "mimetype": mime_type,
                        "size": file_size,
                    }
                    content["org.matrix.msc1767.text"] = attachment.display_name or file_path.name
                    content["org.matrix.msc3245.voice"] = {}

                response = await self._client.room_send(
                    room_id=room_id,
                    message_type="m.room.message",
                    content=content,
                )
                if isinstance(response, RoomSendError):
                    logger.error(f"Matrix attachment send failed room_id={room_id}: {response.message}")
                else:
                    logger.info(f"Sent Matrix attachment room_id={room_id} msgtype={msgtype} path={file_path.name}")
                    if start_new_thread and thread_root is None:
                        thread_root = getattr(response, "event_id", None)
            except Exception as exc:
                self._log_matrix_failure(
                    action="attachment send failed",
                    exc=exc,
                    room_id=room_id,
                    suppression_key=f"send_attachment:{room_id}:{file_path.name}",
                )

    @staticmethod
    def _build_reaction_content(target_event_id: str, key: str) -> dict[str, object]:
        return {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": target_event_id,
                "key": key,
            }
        }

    async def _send_reaction(self, room_id: str, target_event_id: str) -> str | None:
        try:
            response = await self._client.room_send(
                room_id=room_id,
                message_type="m.reaction",
                content=self._build_reaction_content(target_event_id, _PRESENCE_REACTION_KEY),
            )
        except Exception as exc:
            self._log_matrix_failure(
                action="reaction send failed",
                exc=exc,
                room_id=room_id,
                suppression_key=f"reaction:{room_id}",
            )
            return None
        if isinstance(response, RoomSendError):
            logger.error(f"Matrix reaction send failed room_id={room_id}: {response.message}")
            return None
        return getattr(response, "event_id", None)

    async def _redact_event(self, room_id: str, event_id: str) -> None:
        try:
            response = await self._client.room_redact(room_id, event_id)
        except Exception as exc:
            self._log_matrix_failure(
                action="reaction redact failed",
                exc=exc,
                room_id=room_id,
                suppression_key=f"redact:{room_id}",
            )
            return
        if isinstance(response, RoomRedactError):
            logger.error(f"Matrix reaction redact failed room_id={room_id}: {response.message}")

    async def _send_typing_presence(self, room_id: str) -> None:
        for attempt in range(_MAX_TYPING_ACTIONS):
            try:
                await self._client.room_typing(room_id, typing_state=True, timeout=_TYPING_TIMEOUT_MS)
            except Exception as exc:
                self._log_matrix_failure(
                    action="typing status failed",
                    exc=exc,
                    room_id=room_id,
                    suppression_key=f"typing:{room_id}",
                )

            if attempt == _MAX_TYPING_ACTIONS - 1:
                return

            await asyncio.sleep(_TYPING_INTERVAL_SECONDS)

    async def _handle_room_event(self, room_id: str, event: object) -> InboundMessage | None:
        if not isinstance(event, _MESSAGE_EVENT_TYPES):
            return None

        thread_root_event_id: str | None = None
        if self._settings.reply_in_thread:
            thread_root_event_id = _extract_thread_root(event) or event.event_id

        conversation_id = _build_conversation_id(room_id, thread_root_event_id)
        register_channel_conversation(self.name, conversation_id)

        message_text = ""
        metadata: dict[str, object] = {
            "source": "matrix",
            "matrix_room_id": room_id,
            "matrix_event_id": event.event_id,
            "matrix_sender": event.sender,
            "matrix_server_timestamp": event.server_timestamp,
            "matrix_thread_root_event_id": thread_root_event_id,
        }

        if isinstance(event, RoomMessageText):
            message_text = event.body.strip()
        elif isinstance(event, _ATTACHMENT_EVENT_TYPES):
            attachment_context = await self._download_attachment(room_id, event)
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
            conversation_id=conversation_id,
            user_id=event.sender,
            text=message_text,
            metadata=metadata,
        )

    async def _download_attachment(self, room_id: str, event: object) -> dict[str, object] | None:
        body = getattr(event, "body", None) or ""
        url = getattr(event, "url", None)
        source_content = getattr(event, "source", None)
        if isinstance(source_content, dict):
            event_content = source_content.get("content", {})
        else:
            event_content = {}

        info = event_content.get("info", {}) if isinstance(event_content, dict) else {}
        mime_type = info.get("mimetype") if isinstance(info, dict) else None
        original_file_name = body if isinstance(body, str) and body.strip() else None

        msgtype = event_content.get("msgtype", "") if isinstance(event_content, dict) else ""
        kind = _MSGTYPE_TO_KIND.get(msgtype, "file")
        caption_text = ""

        base_metadata: dict[str, object] = {
            "matrix_attachment_type": kind,
            "matrix_attachment_original_file_name": original_file_name,
            "matrix_attachment_mime_type": mime_type,
        }

        if not isinstance(url, str) or not url.startswith("mxc://"):
            return {
                "text": _render_attachment_text(
                    kind=kind,
                    caption_text=caption_text,
                    workspace_path=None,
                    original_file_name=original_file_name,
                    mime_type=mime_type,
                    download_failed=True,
                ),
                "metadata": {
                    **base_metadata,
                    "matrix_attachment_download_error": "missing or invalid mxc:// URL",
                },
            }

        try:
            download_response = await self._client.download(url)

            if hasattr(download_response, "body") and isinstance(download_response.body, bytes):
                file_data = download_response.body
            else:
                raise ValueError("Matrix download response did not contain file data")

            safe_event_id = sanitize_filename(getattr(event, "event_id", "attachment"))
            file_stem = sanitize_filename(Path(original_file_name or kind).stem)

            extension = ""
            if original_file_name:
                extension = Path(original_file_name).suffix
            if not extension and mime_type:
                extension = mimetypes.guess_extension(mime_type, strict=False) or ""

            download_filename = f"{safe_event_id}_{file_stem}{extension}"
            safe_room_id = sanitize_filename(room_id)
            target_path = _MATRIX_DOWNLOADS_DIR / safe_room_id / download_filename

            await asyncio.to_thread(target_path.parent.mkdir, parents=True, exist_ok=True)
            bytes_saved = await async_write_bytes_file(target_path, file_data)
            workspace_path = display_path(target_path, settings.WORKSPACE_ROOT)

            logger.info(
                f"Saved Matrix attachment room_id={room_id} event_id={getattr(event, 'event_id', '?')} path={workspace_path}"
            )

            return {
                "text": _render_attachment_text(
                    kind=kind,
                    caption_text=caption_text,
                    workspace_path=workspace_path,
                    original_file_name=original_file_name,
                    mime_type=mime_type,
                    download_failed=False,
                ),
                "metadata": {
                    **base_metadata,
                    "inbound_attachments": [
                        {
                            "path": workspace_path,
                            "mime_type": mime_type,
                            "display_name": original_file_name,
                            "source": "matrix",
                            "kind": kind,
                        }
                    ],
                    "matrix_attachment_download_path": workspace_path,
                    "matrix_attachment_bytes_saved": bytes_saved,
                },
            }
        except Exception as exc:
            event_id = getattr(event, "event_id", "?")
            self._log_matrix_failure(
                action="attachment download failed",
                exc=exc,
                room_id=room_id,
                suppression_key=f"attachment:{room_id}:{event_id}",
            )
            return {
                "text": _render_attachment_text(
                    kind=kind,
                    caption_text=caption_text,
                    workspace_path=None,
                    original_file_name=original_file_name,
                    mime_type=mime_type,
                    download_failed=True,
                ),
                "metadata": {
                    **base_metadata,
                    "matrix_attachment_download_error": str(exc),
                },
            }

    def _log_matrix_failure(
        self,
        *,
        action: str,
        exc: BaseException,
        room_id: str | None = None,
        suppression_key: str,
    ) -> None:
        context: dict[str, object] = {}
        if room_id is not None:
            context["room_id"] = room_id

        self._failure_logger.log(
            action=action,
            exc=exc,
            suppression_key=suppression_key,
            context=context,
        )


def create_channel() -> MatrixChannel:
    return MatrixChannel()
