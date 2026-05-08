"""Socket.IO-based websocket channel plugin for Pillbug."""

import asyncio
import re
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import socketio
import uvicorn

from app.core.log import logger
from app.runtime.channels import BaseChannel, register_channel_conversation
from app.schema.messages import InboundMessage, OutboundAttachment
from pillbug_websocket.config import settings

_ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_AUTH_HEADER_ENV_KEY = "HTTP_AUTHORIZATION"
_SESSION_ID_HEADER_ENV_KEY = "HTTP_X_SESSIONID"
_BEARER_PREFIX = "Bearer "


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

    def telemetry_details(self) -> dict[str, Any]:
        return {
            "host": settings.HOST,
            "port": settings.PORT,
            "socketio_path": settings.SOCKETIO_PATH,
            "idle_timeout_seconds": settings.IDLE_TIMEOUT_SECONDS,
            "active_sessions": len(self._session_to_sids),
            "active_sockets": len(self._sid_to_session),
        }

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

        logger.info(f"Websocket disconnected sid={sid} session_id={session_id}")

    async def _on_message(self, sid: str, data: object) -> None:
        session_id = self._sid_to_session.get(sid)
        if session_id is None:
            logger.warning(f"Websocket message from unknown sid={sid}; ignoring")
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
            logger.info(f"Websocket session expired due to inactivity session_id={session_id}")


def create_channel() -> WebSocketChannel:
    return WebSocketChannel()
