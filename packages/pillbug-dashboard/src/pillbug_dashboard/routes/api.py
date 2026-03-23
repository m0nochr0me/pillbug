"""JSON API routes for the dashboard browser client."""

import json
from typing import cast

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from pillbug_dashboard.schema import (
    OutboundMessageRequest,
    RegistryMutationResponse,
    RuntimeDetailSnapshot,
    RuntimeOverviewCollection,
    RuntimeRegistration,
    RuntimeRegistrationUpsert,
)
from pillbug_dashboard.services.runtime_client import RuntimeClient, RuntimeClientError
from pillbug_dashboard.services.runtime_hub import RuntimeHub

router = APIRouter(tags=["api"])


def _runtime_client(request: Request) -> RuntimeClient:
    return cast("RuntimeClient", request.app.state.runtime_client)


def _runtime_hub(request: Request) -> RuntimeHub:
    return cast("RuntimeHub", request.app.state.runtime_hub)


def _get_registration_or_404(request: Request, runtime_id: str) -> RuntimeRegistration:
    try:
        return _runtime_hub(request).get_registration(runtime_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _require_control_token(registration: RuntimeRegistration) -> str:
    token = registration.dashboard_bearer_token_value()
    if token is None:
        raise HTTPException(
            status_code=400,
            detail=f"Runtime {registration.runtime_id} does not include a dashboard bearer token.",
        )
    return token


def _extract_response_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])

    return f"Runtime request failed with HTTP {response.status_code}."


async def _proxy_control_action(
    request: Request,
    runtime_id: str,
    *,
    path: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    registration = _get_registration_or_404(request, runtime_id)
    token = _require_control_token(registration)
    try:
        return await _runtime_client(request).post_control_action(
            base_url=registration.base_url,
            path=path,
            bearer_token=token,
            payload=payload,
        )
    except RuntimeClientError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/runtimes", response_model=RuntimeOverviewCollection)
async def list_runtimes(request: Request) -> RuntimeOverviewCollection:
    return await _runtime_hub(request).build_overviews()


@router.post("/runtimes", response_model=RegistryMutationResponse)
async def upsert_runtime(payload: RuntimeRegistrationUpsert, request: Request) -> RegistryMutationResponse:
    return _runtime_hub(request).upsert_registration(payload)


@router.delete("/runtimes/{runtime_id}", response_model=RegistryMutationResponse)
async def delete_runtime(runtime_id: str, request: Request) -> RegistryMutationResponse:
    try:
        return _runtime_hub(request).delete_registration(runtime_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runtimes/{runtime_id}", response_model=RuntimeDetailSnapshot)
async def get_runtime_detail(runtime_id: str, request: Request) -> RuntimeDetailSnapshot:
    try:
        return await _runtime_hub(request).build_detail(runtime_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runtimes/{runtime_id}/agent-card")
async def get_runtime_agent_card(runtime_id: str, request: Request) -> dict[str, object]:
    registration = _get_registration_or_404(request, runtime_id)

    try:
        agent_card = await _runtime_client(request).get_public_agent_card(registration.base_url)
    except RuntimeClientError as exc:
        raise HTTPException(status_code=exc.status_code or 502, detail=str(exc)) from exc

    if agent_card is None:
        raise HTTPException(
            status_code=404,
            detail=f"Runtime {registration.runtime_id} does not expose a public agent card.",
        )

    return agent_card


@router.get("/runtimes/{runtime_id}/events")
async def stream_runtime_events(request: Request, runtime_id: str, replay: int = 20) -> StreamingResponse:
    registration = _get_registration_or_404(request, runtime_id)
    headers = _runtime_client(request).build_headers(registration.dashboard_bearer_token_value())
    replay = max(0, min(replay, 100))

    async def event_stream():
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        try:
            async with (
                httpx.AsyncClient(base_url=registration.base_url.rstrip("/"), timeout=timeout) as client,
                client.stream(
                    "GET",
                    "/telemetry/events",
                    headers=headers,
                    params={"replay": replay},
                ) as response,
            ):
                if response.status_code >= 400:
                    error_payload = {
                        "runtime_id": registration.runtime_id,
                        "detail": _extract_response_error_detail(response),
                        "status_code": response.status_code,
                    }
                    yield f"event: dashboard.error\ndata: {json.dumps(error_payload)}\n\n"
                    return

                async for chunk in response.aiter_text():
                    if await request.is_disconnected():
                        break
                    if chunk:
                        yield chunk
        except Exception as exc:
            error_payload = {
                "runtime_id": registration.runtime_id,
                "detail": str(exc),
            }
            yield f"event: dashboard.error\ndata: {json.dumps(error_payload)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runtimes/{runtime_id}/control/messages/send")
async def send_control_message(runtime_id: str, payload: OutboundMessageRequest, request: Request) -> dict[str, object]:
    return await _proxy_control_action(
        request,
        runtime_id,
        path="/control/messages/send",
        payload=payload.model_dump(mode="json", exclude_none=True),
    )


@router.post("/runtimes/{runtime_id}/control/sessions/{session_id}/clear")
async def clear_runtime_session(runtime_id: str, session_id: str, request: Request) -> dict[str, object]:
    return await _proxy_control_action(request, runtime_id, path=f"/control/sessions/{session_id}/clear")


@router.post("/runtimes/{runtime_id}/control/tasks/{task_id}/enable")
async def enable_runtime_task(runtime_id: str, task_id: str, request: Request) -> dict[str, object]:
    return await _proxy_control_action(request, runtime_id, path=f"/control/tasks/{task_id}/enable")


@router.post("/runtimes/{runtime_id}/control/tasks/{task_id}/disable")
async def disable_runtime_task(runtime_id: str, task_id: str, request: Request) -> dict[str, object]:
    return await _proxy_control_action(request, runtime_id, path=f"/control/tasks/{task_id}/disable")


@router.post("/runtimes/{runtime_id}/control/tasks/{task_id}/run-now")
async def run_runtime_task_now(runtime_id: str, task_id: str, request: Request) -> dict[str, object]:
    return await _proxy_control_action(request, runtime_id, path=f"/control/tasks/{task_id}/run-now")


@router.post("/runtimes/{runtime_id}/control/runtime/drain")
async def drain_runtime(runtime_id: str, request: Request) -> dict[str, object]:
    return await _proxy_control_action(request, runtime_id, path="/control/runtime/drain")


@router.post("/runtimes/{runtime_id}/control/runtime/shutdown")
async def shutdown_runtime(runtime_id: str, request: Request) -> dict[str, object]:
    return await _proxy_control_action(request, runtime_id, path="/control/runtime/shutdown")
