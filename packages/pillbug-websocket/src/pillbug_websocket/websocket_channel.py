"""Socket.IO-based websocket channel plugin for Pillbug."""

import asyncio
import base64
import mimetypes
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import socketio
import uvicorn

from app.core.config import settings as core_settings
from app.core.log import logger
from app.runtime.channel_helpers import render_attachment_text
from app.runtime.channels import BaseChannel, register_channel_conversation, unregister_channel_conversation
from app.schema.messages import InboundMessage, OutboundAttachment
from app.util.workspace import async_write_bytes_file, resolve_path_within_root
from pillbug_websocket.config import settings

_ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_AUTH_HEADER_ENV_KEY = "HTTP_AUTHORIZATION"
_SESSION_ID_HEADER_ENV_KEY = "HTTP_X_SESSIONID"
_BEARER_PREFIX = "Bearer "
_AUDIO_SUFFIX_PATTERN = re.compile(r"\.[A-Za-z0-9]{1,8}")


class _ConnectionRefused(socketio.exceptions.ConnectionRefusedError):
    """Local alias keeping import surface narrow."""


def _normalize_cors_origins(value: str) -> str | list[str]:
    stripped = value.strip()
    if stripped == "*" or not stripped:
        return "*"

    return [origin.strip() for origin in stripped.split(",") if origin.strip()]


def _is_valid_ulid(value: str) -> bool:
    return bool(_ULID_PATTERN.fullmatch(value))


class WebSocketChannel(BaseChannel):
    """Socket.IO channel where each X-SessionID ULID maps to a Pillbug conversation."""

    name = "websocket"
    destination_kind = "session_id"

    def __init__(self) -> None:
        self._inbound_queue: asyncio.Queue[InboundMessage | None] = asyncio.Queue()
        self._sio = socketio.AsyncServer(
            async_mode="asgi",
            cors_allowed_origins=_normalize_cors_origins(settings.CORS_ALLOWED_ORIGINS),
        )
        self._asgi_app = socketio.ASGIApp(self._sio, socketio_path=settings.SOCKETIO_PATH)

        self._sid_to_session: dict[str, str] = {}
        self._session_to_sids: dict[str, set[str]] = {}
        self._session_last_activity: dict[str, float] = {}

        self._server_task: asyncio.Task[None] | None = None
        self._janitor_task: asyncio.Task[None] | None = None
        self._closed = False

        self._sio.on("connect", self._on_connect)
        self._sio.on("disconnect", self._on_disconnect)
        self._sio.on("message", self._on_message)

    async def listen(self) -> AsyncIterator[InboundMessage]:
        ready = asyncio.Event()
        self._server_task = asyncio.create_task(self._run_server(ready), name="websocket-server")
        self._janitor_task = asyncio.create_task(self._run_janitor(), name="websocket-janitor")
        await ready.wait()
        logger.info(
            "Websocket channel listening "
            f"host={settings.HOST} port={settings.PORT} "
            f"socketio_path={settings.SOCKETIO_PATH} idle_timeout={settings.IDLE_TIMEOUT_SECONDS}s"
        )

        try:
            while True:
                message = await self._inbound_queue.get()
                if message is None:
                    return
                yield message
        except asyncio.CancelledError:
            return

    async def send_message(
        self,
        conversation_id: str,
        message_text: str,
        metadata: dict[str, object] | None = None,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        del metadata, attachments
        session_id = conversation_id.strip().upper()
        sids = list(self._session_to_sids.get(session_id, ()))
        if not sids:
            logger.warning(f"Websocket has no active sids for session {session_id}; dropping outbound message")
            return

        payload = {"session_id": session_id, "text": message_text}
        for sid in sids:
            try:
                await self._sio.emit("message", payload, to=sid)
            except Exception as exc:
                logger.warning(f"Websocket emit failed sid={sid} session_id={session_id}: {exc}")

        self._session_last_activity[session_id] = time.monotonic()

    async def send_response(
        self,
        inbound_message: InboundMessage,
        response_text: str,
        attachments: tuple[OutboundAttachment, ...] | None = None,
    ) -> None:
        await self.send_message(
            inbound_message.conversation_id,
            response_text,
            attachments=attachments,
        )

    @asynccontextmanager
    async def stream_response(
        self,
        inbound_message: InboundMessage,
    ) -> AsyncIterator[Callable[[str], Awaitable[None]]]:
        """Optional streaming capability used by ApplicationLoop when PB_STREAMING_CHANNELS
        includes `websocket`. Deltas go out as `stream` events; on clean exit the full text
        is re-sent as the terminal `message` event so clients that ignore `stream` events
        keep working unchanged."""
        session_id = inbound_message.conversation_id.strip().upper()
        streamed_parts: list[str] = []

        async def emit(delta: str) -> None:
            if not delta:
                return
            sids = list(self._session_to_sids.get(session_id, ()))
            if not sids:
                raise RuntimeError(f"no active websocket sids for session {session_id}")

            payload = {"session_id": session_id, "delta": delta}
            for sid in sids:
                try:
                    await self._sio.emit("stream", payload, to=sid)
                except Exception as exc:
                    logger.warning(f"Websocket stream emit failed sid={sid} session_id={session_id}: {exc}")

            streamed_parts.append(delta)
            self._session_last_activity[session_id] = time.monotonic()

        yield emit
        if streamed_parts:
            await self.send_message(session_id, "".join(streamed_parts))

    def telemetry_details(self) -> dict[str, Any]:
        return {
            "host": settings.HOST,
            "port": settings.PORT,
            "socketio_path": settings.SOCKETIO_PATH,
            "idle_timeout_seconds": settings.IDLE_TIMEOUT_SECONDS,
            "active_sessions": len(self._session_to_sids),
            "active_sockets": len(self._sid_to_session),
        }

    def context_destinations(self, known_destinations: tuple[str, ...]) -> tuple[str, ...]:
        # Websocket sessions are ephemeral and reply-in-session: the originating turn already
        # carries its own session id, and any stored ULID goes stale the moment the idle
        # janitor disconnects it. Advertise only the `websocket:<session_id>` placeholder
        # instead of enumerating live sessions as outbound targets.
        del known_destinations
        return ()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._janitor_task is not None:
            self._janitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._janitor_task
            self._janitor_task = None

        for sid in list(self._sid_to_session):
            try:
                await self._sio.disconnect(sid)
            except Exception:
                pass

        if self._server_task is not None:
            self._server_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._server_task
            self._server_task = None

        await self._inbound_queue.put(None)
        logger.info("Websocket channel closed")

    # --- Socket.IO event handlers ---

    async def _on_connect(self, sid: str, environ: dict[str, Any], auth: object | None = None) -> bool:
        del auth

        authorization = str(environ.get(_AUTH_HEADER_ENV_KEY, "") or "").strip()
        expected_token = settings.BEARER_TOKEN.get_secret_value()
        if (
            not authorization.startswith(_BEARER_PREFIX)
            or authorization[len(_BEARER_PREFIX) :].strip() != expected_token
        ):
            logger.warning(f"Websocket connection refused: invalid bearer token sid={sid}")
            raise _ConnectionRefused("invalid bearer token")

        raw_session_id = str(environ.get(_SESSION_ID_HEADER_ENV_KEY, "") or "").strip().upper()
        if not _is_valid_ulid(raw_session_id):
            logger.warning(f"Websocket connection refused: invalid X-SessionID sid={sid}")
            raise _ConnectionRefused("missing or invalid X-SessionID header (expected ULID)")

        self._sid_to_session[sid] = raw_session_id
        self._session_to_sids.setdefault(raw_session_id, set()).add(sid)
        self._session_last_activity[raw_session_id] = time.monotonic()
        register_channel_conversation(self.name, raw_session_id)

        logger.info(f"Websocket connected sid={sid} session_id={raw_session_id}")
        return True

    async def _on_disconnect(self, sid: str) -> None:
        session_id = self._sid_to_session.pop(sid, None)
        if session_id is None:
            return

        sids = self._session_to_sids.get(session_id)
        if sids is not None:
            sids.discard(sid)
            if not sids:
                self._session_to_sids.pop(session_id, None)
                self._session_last_activity.pop(session_id, None)
                unregister_channel_conversation(self.name, session_id)

        logger.info(f"Websocket disconnected sid={sid} session_id={session_id}")

    async def _on_message(self, sid: str, data: object) -> None:
        session_id = self._sid_to_session.get(sid)
        if session_id is None:
            logger.warning(f"Websocket message from unknown sid={sid}; ignoring")
            return

        if isinstance(data, dict) and isinstance(data.get("audio"), dict):
            await self._handle_audio_message(sid, session_id, data)
            return

        text = self._extract_message_text(data)
        if not text:
            return

        self._session_last_activity[session_id] = time.monotonic()

        await self._inbound_queue.put(
            InboundMessage(
                channel_name=self.name,
                conversation_id=session_id,
                user_id=f"ws:{session_id}",
                text=text,
                metadata={
                    "source": "websocket",
                    "websocket_sid": sid,
                    "websocket_session_id": session_id,
                },
            )
        )

    async def _handle_audio_message(self, sid: str, session_id: str, data: dict[str, Any]) -> None:
        """Accept a base64 audio payload, store it under the channel inbox sub-root, and enqueue
        it as a normal inbound attachment. Native audio understanding requires the real Gemini
        backend, so a configured proxy (`PB_GEMINI_BASE_URL`) fails fast rather than forwarding
        audio that a proxy upstream cannot interpret."""
        audio = data["audio"]

        mime_type = str(audio.get("mime_type", "")).strip().lower()
        if not mime_type.startswith("audio/"):
            await self._emit_error(sid, session_id, "unsupported audio payload; mime_type must be audio/*")
            return

        if core_settings.GEMINI_BASE_URL is not None:
            logger.warning(
                f"Websocket audio rejected: a proxy backend (PB_GEMINI_BASE_URL) is configured session_id={session_id}"
            )
            await self._emit_error(
                sid,
                session_id,
                "audio input requires the real Gemini backend; a proxy (PB_GEMINI_BASE_URL) is configured",
            )
            return

        raw_data = audio.get("data")
        if not isinstance(raw_data, str) or not raw_data.strip():
            await self._emit_error(sid, session_id, "audio payload missing base64 data")
            return

        try:
            audio_bytes = base64.b64decode(raw_data, validate=True)
        except ValueError:
            await self._emit_error(sid, session_id, "audio data is not valid base64")
            return

        if not audio_bytes:
            await self._emit_error(sid, session_id, "audio data is empty")
            return
        if len(audio_bytes) > settings.MAX_AUDIO_BYTES:
            await self._emit_error(sid, session_id, f"audio exceeds the limit of {settings.MAX_AUDIO_BYTES} bytes")
            return

        sub_root = core_settings.inbound_attachment_roots().get(self.name, "inbox/websocket")
        display_name = self._audio_display_name(audio.get("filename"))
        extension = self._audio_extension(audio.get("filename"), mime_type)
        relative_path = f"{sub_root}/{session_id}-{time.time_ns()}{extension}"

        try:
            target_path = resolve_path_within_root(relative_path, core_settings.WORKSPACE_ROOT)
            await asyncio.to_thread(target_path.parent.mkdir, parents=True, exist_ok=True)
            await async_write_bytes_file(target_path, audio_bytes)
        except (ValueError, OSError) as exc:
            logger.warning(f"Websocket audio write failed session_id={session_id}: {exc}")
            await self._emit_error(sid, session_id, "failed to store audio attachment")
            return

        workspace_path = target_path.relative_to(core_settings.WORKSPACE_ROOT).as_posix()
        raw_caption = data.get("text")
        caption = raw_caption.strip() if isinstance(raw_caption, str) else ""
        message_text = render_attachment_text(
            channel_label="Websocket",
            attachment_label="audio",
            caption_text=caption,
            workspace_path=workspace_path,
            original_file_name=display_name,
            mime_type=mime_type,
            download_failed=False,
        )

        self._session_last_activity[session_id] = time.monotonic()
        await self._inbound_queue.put(
            InboundMessage(
                channel_name=self.name,
                conversation_id=session_id,
                user_id=f"ws:{session_id}",
                text=message_text,
                metadata={
                    "source": "websocket",
                    "websocket_sid": sid,
                    "websocket_session_id": session_id,
                    "inbound_attachments": [
                        {
                            "path": workspace_path,
                            "mime_type": mime_type,
                            "display_name": display_name,
                            "source": self.name,
                            "kind": "audio",
                        }
                    ],
                },
            )
        )
        logger.info(
            f"Websocket audio stored session_id={session_id} path={workspace_path} "
            f"mime_type={mime_type} bytes={len(audio_bytes)}"
        )

    async def _emit_error(self, sid: str, session_id: str, message: str) -> None:
        payload = {"session_id": session_id, "error": message}
        try:
            await self._sio.emit("error", payload, to=sid)
        except Exception as exc:
            logger.warning(f"Websocket error emit failed sid={sid} session_id={session_id}: {exc}")

    @staticmethod
    def _audio_display_name(raw_filename: object) -> str | None:
        if isinstance(raw_filename, str) and raw_filename.strip():
            return Path(raw_filename.strip()).name or None
        return None

    @staticmethod
    def _audio_extension(raw_filename: object, mime_type: str) -> str:
        if isinstance(raw_filename, str) and (suffix := Path(raw_filename.strip()).suffix):
            if _AUDIO_SUFFIX_PATTERN.fullmatch(suffix):
                return suffix.lower()
        return mimetypes.guess_extension(mime_type) or ".bin"

    @staticmethod
    def _extract_message_text(data: object) -> str:
        if isinstance(data, str):
            return data.strip()
        if isinstance(data, dict):
            text = data.get("text")
            if isinstance(text, str):
                return text.strip()
        return ""

    # --- Internal helpers ---

    async def _run_server(self, ready: asyncio.Event) -> None:
        config = uvicorn.Config(
            self._asgi_app,
            host=settings.HOST,
            port=settings.PORT,
            log_level="warning",
        )
        server = uvicorn.Server(config)

        original_startup = server.startup

        async def _startup_with_signal(sockets: list | None = None) -> None:
            await original_startup(sockets)
            ready.set()

        server.startup = _startup_with_signal  # type: ignore[assignment]
        await server.serve()

    async def _run_janitor(self) -> None:
        try:
            while True:
                await asyncio.sleep(settings.JANITOR_INTERVAL_SECONDS)
                await self._evict_idle_sessions()
        except asyncio.CancelledError:
            return

    async def _evict_idle_sessions(self) -> None:
        now = time.monotonic()
        idle_cutoff = settings.IDLE_TIMEOUT_SECONDS
        stale_sessions = [
            session_id
            for session_id, last_seen in list(self._session_last_activity.items())
            if now - last_seen > idle_cutoff
        ]

        for session_id in stale_sessions:
            sids = list(self._session_to_sids.get(session_id, ()))
            for sid in sids:
                try:
                    await self._sio.disconnect(sid)
                except Exception as exc:
                    logger.warning(f"Websocket janitor disconnect failed sid={sid} session_id={session_id}: {exc}")
            self._session_last_activity.pop(session_id, None)
            self._session_to_sids.pop(session_id, None)
            unregister_channel_conversation(self.name, session_id)
            logger.info(f"Websocket session expired due to inactivity session_id={session_id}")


def create_channel() -> WebSocketChannel:
    return WebSocketChannel()
