"""HTTP server that receives trigger events and feeds them into the debounce buffer."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pillbug_trigger.config import get_trigger_source_config_map, settings
from pillbug_trigger.schema import (
    URGENCY_DEBOUNCE_SECONDS,
    TriggerEvent,
    TriggerResponse,
    TriggerSourceConfig,
    Urgency,
)

_bearer_scheme = HTTPBearer()


def _verify_token(credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer_scheme)]) -> None:
    if credentials.credentials != settings.BEARER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid bearer token")


EventCallback = Callable[[TriggerEvent, TriggerSourceConfig | None, float], Coroutine[Any, Any, None]]


def create_trigger_app(on_event: EventCallback) -> FastAPI:
    """Build the FastAPI app that receives trigger events.

    ``on_event`` is called with (event, source_config_or_none, debounce_seconds)
    for every accepted event.
    """
    app = FastAPI(title="Pillbug Trigger Receiver", docs_url=None, redoc_url=None)

    @app.post("/trigger", response_model=TriggerResponse, dependencies=[Depends(_verify_token)])
    async def receive_trigger(event: TriggerEvent, request: Request) -> TriggerResponse:
        source_cfg = get_trigger_source_config_map().get(event.source)
        urgency = event.urgency
        if source_cfg and source_cfg.urgency_override is not None:
            urgency = source_cfg.urgency_override

        debounce_secs = URGENCY_DEBOUNCE_SECONDS[urgency]

        pending_count = request.app.state.pending_counts.get(_debounce_key(event, urgency), 0) + 1
        request.app.state.pending_counts[_debounce_key(event, urgency)] = pending_count

        await on_event(event, source_cfg, debounce_secs)

        return TriggerResponse(
            event_count=pending_count,
            debounce_seconds=debounce_secs,
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.state.pending_counts = {}

    return app


def _debounce_key(event: TriggerEvent, urgency: Urgency) -> str:
    conversation = event.conversation_id or event.source
    return f"{event.source}:{conversation}:{urgency}"


async def run_server(app: FastAPI, ready: asyncio.Event | None = None) -> None:
    """Start the trigger HTTP server. Sets ``ready`` once listening."""
    config = uvicorn.Config(app, host=settings.HOST, port=settings.PORT, log_level="warning")
    server = uvicorn.Server(config)

    if ready is not None:
        original_startup = server.startup

        async def _startup_with_signal(sockets: list | None = None) -> None:
            await original_startup(sockets)
            ready.set()

        server.startup = _startup_with_signal  # type: ignore[assignment]

    await server.serve()
